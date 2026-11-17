"""Matched-arm comparison of retained evaluation runs (offline only).

This module is the offline half of the M6/WP8 evaluation slice.  It ingests
retained run or result records -- either the explicit record shape documented
in ``docs/eval_compare.md`` or an existing eval report with a ``runs`` list --
and builds a bounded, deterministic comparison of matched arms:

* ``plain``: the plain baseline;
* ``prompted``: a well-prompted baseline with equivalent context;
* ``ozagia``: existing Ozagia;
* ``ozagia_map``: Ozagia plus the map/service path.

Nothing here invokes a model, a driver, a network, a target project, a daemon
or a test.  Records are values; the output is a canonical JSON value plus a
readable rendering.  Missing evidence is ``"unknown"`` and never zero, and a
model's self-reported success is recorded but never used as the acceptance
result.  The acceptance result must come from an independent check.

Compatibility is strict and is decided from evidence, not from labels.  A cell
is one ``(task, repeat)`` pair; every compared arm in a cell must agree on the
task's acceptance-criteria digest, the repository revision or snapshot, the
model, the tool-permission digest and the independent check identity.  Every
one of those values must be known.  Across cells, the revision, model,
permissions and check must also be homogeneous.  If any dimension is missing
or different, the comparison is labelled incompatible and no aggregate is
produced.  The module also refuses to invent a pairing: if no cell has every
arm, or only some cells do, that is reported explicitly.

Thresholds are a caller-supplied input.  There are no built-in go/no-go
numbers; with no thresholds the verdict is ``not_evaluated`` with reason
``thresholds_unresolved``.  This module does not start a live run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from ozagia.core import contracts as C

COMPARISON_NAMESPACE = "ozagia.eval_comparison"
COMPARISON_VERSION = "1.0"

ARM_ORDER = ("plain", "prompted", "ozagia", "ozagia_map")
DEFAULT_ARM_ALIASES = {
    "baseline": "plain",
    "control": "plain",
    "no_graph": "plain",
    "plain": "plain",
    "baseline_context": "prompted",
    "context_baseline": "prompted",
    "prompted": "prompted",
    "well_prompted": "prompted",
    "dev_memory": "ozagia",
    "graph": "ozagia",
    "ozagia": "ozagia",
    "graph_intent": "ozagia_map",
    "map": "ozagia_map",
    "map_service": "ozagia_map",
    "ozagia_map": "ozagia_map",
    "ozagia_service": "ozagia_map",
    "service": "ozagia_map",
}
ARM_INDEX = {arm: index for index, arm in enumerate(ARM_ORDER)}

COMPATIBILITY_DIMENSIONS = (
    "task", "acceptance", "revision", "model", "permissions", "check")
SET_DIMENSIONS = ("revision", "model", "permissions", "check")
PAIR_DIMENSIONS = ("acceptance", "revision", "model", "permissions", "check")

CHECK_RESULTS = ("passed", "failed", "invalid", "unknown", "not_assessed")
SELF_REPORT_VALUES = (
    "passed", "failed", "invalid", "unknown", "not_assessed", "claimed",
)
COMPARISON_STATUSES = (
    "empty", "incomplete", "unpaired", "incompatible", "partial",
    "compatible",
)
VERDICT_STATUSES = ("pass", "fail", "not_evaluated")

TIMING_FIELDS = (
    "wall", "setup", "index_cold", "index_repeat", "model", "tool", "other",
    "queue_wait", "readiness_wait", "prefetch", "check", "operator",
)
COUNT_FIELDS = (
    "tool_calls", "tokens_input", "tokens_cached", "tokens_output",
    "cost_micros", "parse_attempts", "parse_failures", "fallbacks",
)
COVERAGE_FIELDS = ("current", "stale", "unknown", "absent")
CRITICAL_PATH_FIELDS = ("predicted", "observed")
PREFETCH_FIELDS = (
    "prepared", "hits", "served", "used", "unused", "discarded", "errors",
    "wasted_ms", "overhead_ms", "estimated_saved_ms",
)
DEFAULT_REFERENCE_ARM = "plain"

DEFAULT_MAX_RECORDS = 512
HARD_MAX_RECORDS = 4096
DEFAULT_MAX_BYTES = 128 * 1024
HARD_MAX_BYTES = 1024 * 1024
MAX_ARMS = len(ARM_ORDER)
MAX_TEXT = 512

ERROR_CODES = frozenset({
    "compare.invalid_input",
    "compare.invalid_record",
    "compare.unknown_arm",
    "compare.duplicate_run",
    "compare.bounds_exceeded",
    "compare.invalid_comparison",
    "compare.invalid_thresholds",
})

COMPARISON_SCHEMA = {
    "namespace": COMPARISON_NAMESPACE,
    "version": COMPARISON_VERSION,
    "types": {
        "comparison": {
            "description": (
                "A bounded matched-arm comparison built from retained run "
                "records. Aggregate and deltas are null unless the compared "
                "cells are compatible."),
            "fields": [
                "schema", "comparison_version", "scope", "status",
                "compatible", "reasons", "arms", "required_arms",
                "compatibility", "cells", "aggregate", "runs", "bounds",
                "omissions", "not_assessed", "thresholds", "verdict", "hash",
                "created_at",
            ],
        },
    },
    "required": [
        "schema", "comparison_version", "scope", "status", "compatible",
        "reasons", "arms", "required_arms", "compatibility", "cells",
        "aggregate", "runs", "bounds", "omissions", "not_assessed",
        "thresholds", "verdict", "hash",
    ],
    "constraints": {
        "max_records": DEFAULT_MAX_RECORDS,
        "hard_max_records": HARD_MAX_RECORDS,
        "max_bytes": DEFAULT_MAX_BYTES,
        "hard_max_bytes": HARD_MAX_BYTES,
        "max_arms": MAX_ARMS,
    },
    "sensitivity": "internal",
}


# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------


class ComparisonError(ValueError):
    """A comparison input or document violation with a stable code."""

    def __init__(self, message: str, *, code: str = "compare.invalid_input") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown comparison error code {code!r}")
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _text(value: Any, name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value:
        raise ComparisonError(f"{name} must be a non-empty string",
                              code="compare.invalid_input")
    if len(value) > limit:
        raise ComparisonError(f"{name} exceeds {limit} characters",
                              code="compare.invalid_input")
    if any(ord(char) < 0x20 for char in value):
        raise ComparisonError(f"{name} contains control characters",
                              code="compare.invalid_input")
    return value


def _optional_text(value: Any, name: str, *, limit: int = MAX_TEXT
                   ) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit=limit)


def _int_or_none(value: Any, name: str, *, minimum: int = 0) -> int | None:
    if value is None or value == "unknown":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComparisonError(f"{name} must be an integer or 'unknown'",
                              code="compare.invalid_record")
    if value < minimum or value > C.INT64_MAX:
        raise ComparisonError(
            f"{name} must be between {minimum} and {C.INT64_MAX}",
            code="compare.invalid_record")
    return value


def _bool_or_none(value: Any, name: str) -> bool | None:
    if value is None or value == "unknown":
        return None
    if value is True or value is False:
        return value
    raise ComparisonError(f"{name} must be true, false or 'unknown'",
                          code="compare.invalid_record")


def _self_report_label(value: Any) -> str | None:
    """Keep a bounded claim label, never a free-form model message."""
    if value is None:
        return None
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    if isinstance(value, str) and value:
        lowered = value.lower()
        if lowered == "unknown":
            return None
        if lowered in SELF_REPORT_VALUES:
            return lowered
        return "claimed"
    raise ComparisonError("record.self_reported must be a bool or string",
                          code="compare.invalid_record")


def _decimal(value: Any, name: str) -> Decimal | None:
    if value is None or value == "unknown":
        return None
    if isinstance(value, bool):
        raise ComparisonError(f"{name} must be numeric", code="compare.invalid_record")
    if isinstance(value, float):
        text = repr(value)
    elif isinstance(value, (int, str, Decimal)):
        text = str(value)
    else:
        raise ComparisonError(f"{name} must be numeric", code="compare.invalid_record")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ComparisonError(f"{name} is not a decimal number",
                              code="compare.invalid_record") from exc
    if not number.is_finite() or number < 0:
        raise ComparisonError(f"{name} must be a finite non-negative number",
                              code="compare.invalid_record")
    return number


def _seconds_to_ms(value: Any, name: str) -> int | None:
    number = _decimal(value, name)
    if number is None:
        return None
    try:
        result = int((number * 1000).quantize(Decimal("1"),
                                              rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ComparisonError(f"{name} is not a bounded decimal number",
                              code="compare.invalid_record") from exc
    if result > C.INT64_MAX:
        raise ComparisonError(f"{name} is outside signed 64-bit range",
                              code="compare.invalid_record")
    return result


def _usd_to_micros(value: Any, name: str) -> int | None:
    number = _decimal(value, name)
    if number is None:
        return None
    try:
        result = int((number * 1_000_000).quantize(Decimal("1"),
                                                   rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ComparisonError(f"{name} is not a bounded decimal number",
                              code="compare.invalid_record") from exc
    if result > C.INT64_MAX:
        raise ComparisonError(f"{name} is outside signed 64-bit range",
                              code="compare.invalid_record")
    return result


def _pick(mapping: Mapping[str, Any] | None, *names: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _nested(mapping: Mapping[str, Any] | None, *path: str) -> Any:
    current: Any = mapping
    for name in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(name)
    return current


def _bounded_value(value: Any, name: str = "value") -> str | None:
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return "sha256:" + C.content_hash(value)
        except C.ContractError as exc:
            raise ComparisonError(f"{name} is not canonical JSON: {exc}",
                                  code="compare.invalid_record") from exc
    return _optional_text(value, name)


def _mean(total: int | None, count: int) -> int | None:
    if total is None or count <= 0:
        return None
    if total >= 0:
        return (total + count // 2) // count
    return -((-total + count // 2) // count)


def _permille(numerator: int | None, denominator: int | None) -> int | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator * 1000 + denominator // 2) // denominator


def _rate_per_hour_milli(successes: int | None, total_ms: int | None
                         ) -> int | None:
    """Verified tasks per hour in thousandths, from successes and summed ms."""
    if successes is None or total_ms is None or total_ms <= 0:
        return None
    return (successes * 3_600_000_000 + total_ms // 2) // total_ms


def _out(value: Any) -> Any:
    return "unknown" if value is None else value


def _accumulate(values: Iterable[int | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    return {
        "total": sum(known) if known else None,
        "known_runs": len(known),
        "unknown_runs": sum(1 for value in values if value is None),
    }


def _metric(values: Iterable[int | None]) -> dict[str, Any]:
    values = list(values)
    accumulated = _accumulate(values)
    return {
        "total": accumulated["total"],
        "known_runs": accumulated["known_runs"],
        "unknown_runs": accumulated["unknown_runs"],
    }


def _out_accumulated(accumulated: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "total": accumulated["total"],
        "known_runs": accumulated["known_runs"],
        "unknown_runs": accumulated["unknown_runs"],
    }


# ---------------------------------------------------------------------------
# Normalized records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonRecord:
    """One retained run, normalized for comparison.

    Unknown scalar evidence is ``None`` in the API and ``"unknown"`` in the
    canonical document.  ``self_reported`` is never used to decide success.
    """

    id: str
    arm: str
    task: str
    repeat: int = 1
    acceptance: str | None = None
    revision: str | None = None
    model: str | None = None
    permissions: str | None = None
    check: str | None = None
    harness: str | None = None
    prefetch_enabled: bool | None = None
    check_result: str = "unknown"
    valid: bool | None = None
    self_reported: str | None = None
    timing: Mapping[str, int | None] = field(default_factory=dict)
    time_to_verified_ms: int | None = None
    counts: Mapping[str, int | None] = field(default_factory=dict)
    coverage: Mapping[str, int] | None = None
    critical_path: Mapping[str, int | None] = field(default_factory=dict)
    prefetch: Mapping[str, int | None] | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        _text(self.id, "record.id", limit=256)
        if self.arm not in ARM_ORDER:
            raise ComparisonError(f"record arm {self.arm!r} is not canonical",
                                  code="compare.unknown_arm")
        _text(self.task, "record.task")
        if isinstance(self.repeat, bool) or not isinstance(self.repeat, int) \
                or self.repeat < 1 or self.repeat > C.INT64_MAX:
            raise ComparisonError("record.repeat must be a positive integer",
                                  code="compare.invalid_record")
        for name in ("acceptance", "revision", "model", "permissions",
                     "check", "harness", "self_reported", "source"):
            _optional_text(getattr(self, name), f"record.{name}")
        if self.self_reported is not None \
                and self.self_reported not in SELF_REPORT_VALUES:
            raise ComparisonError(
                f"record.self_reported must be one of {SELF_REPORT_VALUES}",
                code="compare.invalid_record")
        if self.check_result not in CHECK_RESULTS:
            raise ComparisonError(
                f"record.check_result must be one of {CHECK_RESULTS}",
                code="compare.invalid_record")
        _bool_or_none(self.valid, "record.valid")
        _bool_or_none(self.prefetch_enabled, "record.prefetch_enabled")
        timing = dict(self.timing or {})
        for key in timing:
            if key not in TIMING_FIELDS:
                raise ComparisonError(f"unknown timing field {key!r}",
                                      code="compare.invalid_record")
        for key, value in timing.items():
            timing[key] = _int_or_none(value, f"record.timing.{key}")
        counts = dict(self.counts or {})
        for key in counts:
            if key not in COUNT_FIELDS:
                raise ComparisonError(f"unknown count field {key!r}",
                                      code="compare.invalid_record")
        for key, value in counts.items():
            counts[key] = _int_or_none(value, f"record.counts.{key}")
        object.__setattr__(self, "timing", timing)
        object.__setattr__(self, "counts", counts)
        _int_or_none(self.time_to_verified_ms, "record.time_to_verified_ms")
        coverage = None
        if self.coverage is not None:
            coverage = dict(self.coverage)
            if set(coverage) != set(COVERAGE_FIELDS):
                raise ComparisonError(
                    f"record.coverage must have exactly {COVERAGE_FIELDS}",
                    code="compare.invalid_record")
            for key, value in coverage.items():
                if isinstance(value, bool) or not isinstance(value, int) \
                        or value < 0:
                    raise ComparisonError(
                        f"record.coverage.{key} must be a non-negative integer",
                        code="compare.invalid_record")
        object.__setattr__(self, "coverage", coverage)
        critical = dict(self.critical_path or {})
        for key in critical:
            if key not in CRITICAL_PATH_FIELDS:
                raise ComparisonError(f"unknown critical-path field {key!r}",
                                      code="compare.invalid_record")
        for key, value in critical.items():
            critical[key] = _int_or_none(value,
                                         f"record.critical_path.{key}")
        object.__setattr__(self, "critical_path", critical)
        if self.prefetch is not None:
            prefetch = dict(self.prefetch)
            for key in prefetch:
                if key not in PREFETCH_FIELDS:
                    raise ComparisonError(
                        f"unknown prefetch field {key!r}",
                        code="compare.invalid_record")
            for key, value in prefetch.items():
                prefetch[key] = _int_or_none(value,
                                             f"record.prefetch.{key}")
            object.__setattr__(self, "prefetch", prefetch)

    def to_dict(self) -> dict[str, Any]:
        timing = {f"{key}_ms": _out((self.timing or {}).get(key))
                  for key in TIMING_FIELDS}
        counts = {key: _out((self.counts or {}).get(key))
                  for key in COUNT_FIELDS}
        critical = {
            f"{key}_ms": _out((self.critical_path or {}).get(key))
            for key in CRITICAL_PATH_FIELDS
        }
        predicted = (self.critical_path or {}).get("predicted")
        observed = (self.critical_path or {}).get("observed")
        critical["delta_ms"] = _out(
            None if predicted is None or observed is None
            else observed - predicted)
        prefetch = None
        if self.prefetch is not None:
            prefetch = {key: _out((self.prefetch or {}).get(key))
                        for key in PREFETCH_FIELDS}
        coverage = None
        if self.coverage is not None:
            coverage = {key: self.coverage[key] for key in COVERAGE_FIELDS}
        return {
            "id": self.id,
            "arm": self.arm,
            "task": self.task,
            "repeat": self.repeat,
            "compatibility": {
                "acceptance": _out(self.acceptance),
                "revision": _out(self.revision),
                "model": _out(self.model),
                "permissions": _out(self.permissions),
                "check": _out(self.check),
            },
            "harness": _out(self.harness),
            "prefetch_enabled": (
                "unknown" if self.prefetch_enabled is None
                else self.prefetch_enabled),
            "result": {
                "check": self.check_result,
                "valid": "unknown" if self.valid is None else self.valid,
                "self_reported": _out(self.self_reported),
            },
            "timing_ms": timing,
            "time_to_verified_ms": _out(self.time_to_verified_ms),
            "counts": counts,
            "coverage": "unknown" if coverage is None else coverage,
            "critical_path": critical,
            "prefetch": "unknown" if prefetch is None else prefetch,
            "source": _out(self.source),
        }


def _resolve_arm(value: Any, aliases: Mapping[str, str]) -> str:
    text = _text(value, "arm", limit=128)
    canonical = aliases.get(text, text)
    if canonical not in ARM_ORDER:
        raise ComparisonError(
            f"unknown arm {text!r}; expected one of {ARM_ORDER} or a declared "
            "alias", code="compare.unknown_arm")
    return canonical


def _arm_aliases(overrides: Mapping[str, str] | None) -> dict[str, str]:
    aliases = dict(DEFAULT_ARM_ALIASES)
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise ComparisonError("aliases must be a mapping",
                                  code="compare.invalid_input")
        for key, value in overrides.items():
            _text(key, "alias name", limit=128)
            aliases[key] = _resolve_arm(value, aliases)
    return aliases


def _legacy_timing(value: Mapping[str, Any]) -> Mapping[str, Any]:
    timing = value.get("timing")
    return timing if isinstance(timing, Mapping) else {}


def _normalize_record(
    value: Any,
    *,
    defaults: Mapping[str, Any] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> ComparisonRecord:
    if isinstance(value, ComparisonRecord):
        return value
    if not isinstance(value, Mapping):
        raise ComparisonError("record must be an object",
                              code="compare.invalid_record")
    aliases = aliases or DEFAULT_ARM_ALIASES
    defaults = defaults or {}

    def field_value(*names: str) -> Any:
        found = _pick(value, *names)
        return _pick(defaults, *names) if found is None else found

    arm = _resolve_arm(field_value("arm"), aliases)
    task = field_value("task", "task_id", "task_key")
    if task is None:
        raise ComparisonError("record is missing a task ID",
                              code="compare.invalid_record")
    task = _text(task, "record.task")
    repeat_value = field_value("repeat", "repetition")
    if repeat_value is None:
        repeat = 1
    else:
        repeat = _int_or_none(repeat_value, "record.repeat", minimum=1)
        if repeat is None:
            raise ComparisonError("record.repeat must be a known integer",
                                  code="compare.invalid_record")
    record_id = field_value("id", "run_id", "record_id")
    if record_id is None:
        record_id = f"{arm}:{task}:{repeat}"
    record_id = _text(record_id, "record.id", limit=256)

    legacy = _legacy_timing(value)
    timing_ms = value.get("timing_ms") if isinstance(
        value.get("timing_ms"), Mapping) else {}
    legacy_setup = _nested(legacy, "setup", "s")
    legacy_index = _nested(legacy, "index")
    legacy_model = _nested(legacy, "model", "s")
    legacy_tools = _nested(legacy, "tools", "s")
    legacy_other = _nested(legacy, "other", "s")
    legacy_check = _nested(legacy, "check", "s")
    legacy_wall = _nested(legacy, "wall_s")

    def timing(name: str, *names: str) -> int | None:
        for container in (timing_ms, value):
            if not isinstance(container, Mapping):
                continue
            keys = ((f"{name}_ms", name) if container is timing_ms else names)
            for key in keys:
                if key in container and container[key] is not None:
                    direct = container[key]
                    if isinstance(direct, (Mapping, list, tuple)):
                        continue
                    if key.endswith("_ms") or container is timing_ms:
                        return _int_or_none(direct,
                                            f"record.timing.{name}")
                    return _seconds_to_ms(direct,
                                          f"record.timing.{name}")
        return None

    setup = timing("setup", "setup_ms", "setup_s")
    if setup is None:
        setup = _seconds_to_ms(legacy_setup, "record.timing.setup")
    index_cold = timing("index_cold", "index_cold_ms", "index_ms",
                        "prep_ms", "index_s", "prep_s")
    if index_cold is None:
        index_cold = _seconds_to_ms(
            _nested(legacy_index, "cold_s"), "record.timing.index_cold")
    index_repeat = timing("index_repeat", "index_repeat_ms",
                          "prep_repeat_ms", "index_repeat_s",
                          "prep_repeat_s")
    if index_repeat is None:
        index_repeat = _seconds_to_ms(
            _nested(legacy_index, "repeat_s"),
            "record.timing.index_repeat")
    model = timing("model", "model_ms", "model_s")
    if model is None:
        model = _seconds_to_ms(legacy_model, "record.timing.model")
    tool = timing("tool", "tool_ms", "tools_ms", "tool_s", "tools_s")
    if tool is None:
        tool = _seconds_to_ms(legacy_tools, "record.timing.tool")
    other = timing("other", "other_ms", "other_s")
    if other is None:
        other = _seconds_to_ms(legacy_other, "record.timing.other")
    queue_wait = timing("queue_wait", "queue_wait_ms", "queue_wait_s")
    readiness_wait = timing("readiness_wait", "readiness_wait_ms",
                            "readiness_wait_s")
    prefetch_ms = timing("prefetch", "prefetch_ms", "prefetch_s")
    check_ms = timing("check", "check_ms", "check_s")
    if check_ms is None:
        check_ms = _seconds_to_ms(legacy_check, "record.timing.check")
    operator = timing("operator", "operator_ms", "operator_prep_ms",
                      "operator_prep_s", "operator_s")

    wall = None
    if value.get("wall_ms") is not None:
        wall = _int_or_none(value.get("wall_ms"), "record.wall_ms")
    elif defaults.get("wall_ms") is not None:
        wall = _int_or_none(defaults.get("wall_ms"), "record.wall_ms")
    elif _pick(value, "wall_s") is not None:
        wall = _seconds_to_ms(_pick(value, "wall_s"), "record.wall_s")
    elif _pick(defaults, "wall_s") is not None:
        wall = _seconds_to_ms(_pick(defaults, "wall_s"), "record.wall_s")
    elif _pick(value, "elapsed_ms") is not None:
        wall = _int_or_none(_pick(value, "elapsed_ms"), "record.elapsed_ms")
    elif _pick(value, "elapsed_seconds") is not None:
        wall = _seconds_to_ms(_pick(value, "elapsed_seconds"),
                              "record.elapsed_seconds")
    elif timing_ms.get("wall_ms") is not None:
        wall = _int_or_none(timing_ms.get("wall_ms"), "record.timing.wall_ms")
    elif timing_ms.get("wall") is not None:
        wall = _int_or_none(timing_ms.get("wall"), "record.timing.wall")
    elif legacy_wall is not None:
        wall = _seconds_to_ms(legacy_wall, "record.timing.wall")

    time_to_verified = _pick(value, "time_to_verified_ms")
    if time_to_verified is None:
        time_to_verified = _pick(defaults, "time_to_verified_ms")
    if time_to_verified is None and "time_to_verified_s" in value:
        time_to_verified = _seconds_to_ms(
            value.get("time_to_verified_s"), "record.time_to_verified_s")
    if time_to_verified is not None:
        time_to_verified = _int_or_none(
            time_to_verified, "record.time_to_verified_ms")
    elif legacy:
        if None not in (setup, index_cold, wall, check_ms):
            time_to_verified = setup + index_cold + wall + check_ms
    elif wall is not None:
        time_to_verified = wall

    counts = value.get("counts") if isinstance(value.get("counts"),
                                                Mapping) else {}

    def count(name: str, *names: str) -> int | None:
        direct = _pick(counts, name)
        if direct is None:
            direct = _pick(value, *names)
        if direct is None:
            direct = _pick(defaults, *names)
        return _int_or_none(direct, f"record.counts.{name}")

    tool_calls = count("tool_calls", "tool_calls")
    tokens_input = count("tokens_input", "tokens_input", "tokens_in")
    tokens_cached = count("tokens_cached", "tokens_cached", "cached_tokens")
    tokens_output = count("tokens_output", "tokens_output", "tokens_out")
    cost_micros = _pick(counts, "cost_micros")
    if cost_micros is None:
        cost_micros = _pick(value, "cost_micros")
    if cost_micros is None:
        cost_usd = _pick(value, "cost_usd", "cost")
        cost_micros = (_usd_to_micros(cost_usd, "record.cost_usd")
                       if cost_usd is not None else None)
    else:
        cost_micros = _int_or_none(cost_micros, "record.cost_micros")
    parse_attempts = count("parse_attempts", "parse_attempts")
    parse_failures = count("parse_failures", "parse_failures")
    fallbacks = count("fallbacks", "fallbacks")

    coverage_value = _pick(value, "coverage")
    if coverage_value is None:
        coverage_value = _pick(defaults, "coverage")
    coverage = None
    if isinstance(coverage_value, Mapping):
        inner = coverage_value.get("counts")
        source = inner if isinstance(inner, Mapping) else coverage_value
        present = [key for key in COVERAGE_FIELDS if key in source]
        if present:
            if len(present) != len(COVERAGE_FIELDS):
                raise ComparisonError(
                    "record.coverage must have current, stale, unknown and "
                    "absent counts", code="compare.invalid_record")
            coverage = {}
            for key in COVERAGE_FIELDS:
                value_int = _int_or_none(source.get(key),
                                         f"record.coverage.{key}")
                if value_int is None:
                    raise ComparisonError(
                        f"record.coverage.{key} must be known",
                        code="compare.invalid_record")
                coverage[key] = value_int

    critical_value = _pick(value, "critical_path")
    critical: dict[str, int | None] = {}
    if isinstance(critical_value, Mapping):
        for key in CRITICAL_PATH_FIELDS:
            critical[key] = _int_or_none(
                _pick(critical_value, f"{key}_ms", key),
                f"record.critical_path.{key}")
    for key in CRITICAL_PATH_FIELDS:
        if key not in critical:
            direct = _pick(value, f"{key}_critical_path_ms")
            critical[key] = (_int_or_none(
                direct, f"record.critical_path.{key}")
                if direct is not None else None)

    prefetch_value = _pick(value, "prefetch", "prefetch_ledger")
    prefetch = None
    if isinstance(prefetch_value, Mapping):
        prefetch = {}
        for key in PREFETCH_FIELDS:
            prefetch[key] = _int_or_none(
                prefetch_value.get(key), f"record.prefetch.{key}")

    check_result = _pick(value, "check_result")
    if check_result is None:
        success = value.get("success")
        if success is True or success is False:
            check_result = "passed" if success else "failed"
        elif "check_returncode" in value:
            exit_code = _int_or_none(value.get("check_returncode"),
                                     "record.check_returncode", minimum=0)
            if exit_code is None:
                check_result = "unknown"
            else:
                check_result = "passed" if exit_code == 0 else "failed"
        else:
            check_result = "unknown"
    if check_result not in CHECK_RESULTS:
        raise ComparisonError(
            f"record.check_result must be one of {CHECK_RESULTS}",
            code="compare.invalid_record")

    valid = _pick(value, "valid")
    if valid is None:
        valid = _pick(defaults, "valid")
    valid = _bool_or_none(valid, "record.valid")

    return ComparisonRecord(
        id=record_id,
        arm=arm,
        task=task,
        repeat=repeat,
        acceptance=_bounded_value(
            field_value("acceptance", "acceptance_criteria",
                        "criteria_hash", "task_fingerprint"),
            "record.acceptance"),
        revision=_bounded_value(
            field_value("revision", "snapshot", "base_commit", "commit",
                        "repository_revision"),
            "record.revision"),
        model=_bounded_value(field_value("model", "model_id"),
                              "record.model"),
        permissions=_bounded_value(
            field_value("permissions", "tool_permissions",
                        "permissions_hash"),
            "record.permissions"),
        check=_bounded_value(
            field_value("check", "check_id", "check_hash",
                        "task_check_hash"),
            "record.check"),
        harness=_bounded_value(field_value("harness", "driver"),
                                "record.harness"),
        prefetch_enabled=_bool_or_none(
            field_value("prefetch_enabled"), "record.prefetch_enabled"),
        check_result=check_result,
        valid=valid,
        self_reported=_self_report_label(
            _pick(value, "self_reported", "model_verdict",
                  "claimed_success")),
        timing={
            "wall": wall,
            "setup": setup,
            "index_cold": index_cold,
            "index_repeat": index_repeat,
            "model": model,
            "tool": tool,
            "other": other,
            "queue_wait": queue_wait,
            "readiness_wait": readiness_wait,
            "prefetch": prefetch_ms,
            "check": check_ms,
            "operator": operator,
        },
        time_to_verified_ms=time_to_verified,
        counts={
            "tool_calls": tool_calls,
            "tokens_input": tokens_input,
            "tokens_cached": tokens_cached,
            "tokens_output": tokens_output,
            "cost_micros": cost_micros,
            "parse_attempts": parse_attempts,
            "parse_failures": parse_failures,
            "fallbacks": fallbacks,
        },
        coverage=coverage,
        critical_path=critical,
        prefetch=prefetch,
        source=_optional_text(_pick(value, "source"), "record.source"),
    )


def records_from_report(
    report: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> list[ComparisonRecord]:
    """Normalize an existing eval report with a ``runs`` list.

    Report-level evidence is used as a default: ``base_commit``/``base`` for
    the revision, ``backend.model`` for the model and ``driver`` for the
    harness.  The run record wins when it supplies the same field.  A report
    that does not state the acceptance criteria, tool permissions or check
    identity still leaves those dimensions unknown.
    """
    if not isinstance(report, Mapping):
        raise ComparisonError("report must be an object",
                              code="compare.invalid_input")
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise ComparisonError("report.runs must be a list",
                              code="compare.invalid_input")
    report_defaults: dict[str, Any] = {
        "revision": _pick(report, "base_commit", "base", "revision"),
        "model": _nested(report, "backend", "model")
        if isinstance(report.get("backend"), Mapping)
        else _pick(report, "model"),
        "harness": _pick(report, "driver", "harness"),
    }
    if isinstance(defaults, Mapping):
        report_defaults.update(defaults)
    resolved_aliases = _arm_aliases(aliases)
    return [_normalize_record(run, defaults=report_defaults,
                              aliases=resolved_aliases)
            for run in runs]


def _ingest(
    records: Any,
    *,
    defaults: Mapping[str, Any] | None,
    aliases: Mapping[str, str] | None,
) -> list[ComparisonRecord]:
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except ValueError as exc:
            raise ComparisonError(f"records JSON is invalid: {exc}",
                                  code="compare.invalid_input") from exc
    if isinstance(records, Mapping):
        if isinstance(records.get("runs"), list):
            return records_from_report(records, defaults=defaults,
                                       aliases=aliases)
        records = [records]
    if not isinstance(records, Iterable) or isinstance(records, (bytes,
                                                                  bytearray)):
        raise ComparisonError("records must be a list, object or JSON text",
                              code="compare.invalid_input")
    aliases = _arm_aliases(aliases)
    return [_normalize_record(record, defaults=defaults, aliases=aliases)
            for record in records]


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


def _compat_value(record: ComparisonRecord, dimension: str) -> str | None:
    if dimension == "task":
        return record.task
    value = getattr(record, dimension)
    return None if value is None or value == "unknown" else value


def _record_sort_key(record: ComparisonRecord) -> tuple[Any, ...]:
    return (record.task, record.repeat, ARM_INDEX[record.arm], record.id)


def _dimension_row(
    records: Iterable[ComparisonRecord],
    dimension: str,
    arms: Iterable[str],
) -> dict[str, Any]:
    arms = list(arms)
    by_arm = {record.arm: record for record in records}
    values = []
    known: list[str] = []
    unknown = False
    for arm in arms:
        record = by_arm.get(arm)
        value = _compat_value(record, dimension) if record is not None else None
        if value is None:
            unknown = True
        else:
            known.append(value)
        values.append({"arm": arm, "value": _out(value)})
    if unknown:
        status = "unknown"
    elif len(set(known)) > 1:
        status = "mismatch"
    else:
        status = "match"
    return {"status": status, "values": values}


def _cell_compatibility(
    cell: tuple[str, int],
    records: Iterable[ComparisonRecord],
    arms: Iterable[str],
) -> dict[str, Any]:
    records = list(records)
    dimensions = {dimension: _dimension_row(records, dimension, arms)
                  for dimension in COMPATIBILITY_DIMENSIONS}
    statuses = {row["status"] for row in dimensions.values()}
    if "mismatch" in statuses:
        status = "mismatch"
    elif "unknown" in statuses:
        status = "unknown"
    else:
        status = "match"
    return {
        "task": cell[0],
        "repeat": cell[1],
        "arms": list(arms),
        "status": status,
        "dimensions": dimensions,
    }


def _set_compatibility(
    complete_cells: Mapping[tuple[str, int], Mapping[str, ComparisonRecord]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dimension in SET_DIMENSIONS:
        values: list[str] = []
        unknown = False
        for cell in sorted(complete_cells):
            for arm in sorted(complete_cells[cell], key=lambda a: ARM_INDEX[a]):
                value = _compat_value(complete_cells[cell][arm], dimension)
                if value is None:
                    unknown = True
                elif value not in values:
                    values.append(value)
        if unknown:
            status = "unknown"
        elif len(values) > 1:
            status = "mismatch"
        else:
            status = "match"
        result[dimension] = {
            "status": status,
            "values": values if not unknown else values + ["unknown"],
        }
    return result


def _compatibility_issues(
    cells: Iterable[Mapping[str, Any]],
    set_rows: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for cell in cells:
        for dimension, row in cell["dimensions"].items():
            if row["status"] == "match":
                continue
            issues.append({
                "scope": "cell",
                "task": cell["task"],
                "repeat": cell["repeat"],
                "dimension": dimension,
                "status": row["status"],
                "values": row["values"],
            })
    for dimension, row in set_rows.items():
        if row["status"] == "match":
            continue
        issues.append({
            "scope": "set",
            "task": "unknown",
            "repeat": "unknown",
            "dimension": dimension,
            "status": row["status"],
            "values": row["values"],
        })
    return issues


def _compatibility_reasons(issues: Iterable[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for issue in issues:
        reason = f"{issue['scope']}_{issue['dimension']}_{issue['status']}"
        if reason not in reasons:
            reasons.append(reason)
    return reasons


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _run_verified(record: ComparisonRecord) -> bool:
    return record.valid is True and record.check_result == "passed"


def _arm_metrics(arm: str,
                 records: Iterable[ComparisonRecord]) -> dict[str, Any]:
    records = list(records)
    check_counts = {name: 0 for name in CHECK_RESULTS}
    for record in records:
        check_counts[record.check_result] += 1
    valid_runs = sum(1 for record in records if record.valid is True)
    invalid_runs = sum(1 for record in records if record.valid is False)
    unknown_validity = len(records) - valid_runs - invalid_runs
    decisive = [record for record in records
                if record.valid is True
                and record.check_result in ("passed", "failed")]
    successes = sum(1 for record in decisive
                    if record.check_result == "passed")
    unknown_check_runs = sum(
        1 for record in records
        if record.valid is True
        and record.check_result in ("unknown", "not_assessed"))
    invalid_check_runs = sum(1 for record in records
                             if record.check_result == "invalid")
    verified_times = [record.time_to_verified_ms for record in records
                      if _run_verified(record)]
    known_times = [value for value in verified_times if value is not None]
    time_total = sum(known_times) if known_times else None
    timing = {
        field_name: _metric(
            [record.timing.get(field_name) for record in records])
        for field_name in TIMING_FIELDS
    }
    wall = _metric([record.timing.get("wall") for record in records])
    tool_calls = _metric([record.counts.get("tool_calls")
                          for record in records])
    tool_calls["mean_milli"] = (
        (tool_calls["total"] * 1000 + tool_calls["known_runs"] // 2)
        // tool_calls["known_runs"]
        if tool_calls["total"] is not None and tool_calls["known_runs"] else None)
    tokens = {
        field_name: _metric([record.counts.get(field_name)
                             for record in records])
        for field_name in ("tokens_input", "tokens_cached", "tokens_output")
    }
    token_total = _metric([
        (record.counts.get("tokens_input") + record.counts.get("tokens_output")
         if record.counts.get("tokens_input") is not None
         and record.counts.get("tokens_output") is not None else None)
        for record in records])
    cost = _metric([record.counts.get("cost_micros") for record in records])
    attempts = _accumulate([record.counts.get("parse_attempts")
                            for record in records])
    failures = _accumulate([record.counts.get("parse_failures")
                            for record in records])
    fallback_values = [record.counts.get("fallbacks") for record in records]
    fallbacks = _accumulate(fallback_values)
    fallback_runs = (
        sum(1 for value in fallback_values
            if value is not None and value > 0)
        if fallbacks["known_runs"] else None)
    parse_rate = None
    if attempts["total"] is not None and failures["total"] is not None \
            and attempts["unknown_runs"] == 0 and failures["unknown_runs"] == 0:
        parse_rate = _permille(failures["total"], attempts["total"])
    fallback_rate = _permille(fallback_runs,
                              fallbacks["known_runs"]
                              if fallbacks["unknown_runs"] == 0 else None)
    coverage_known = [record.coverage for record in records
                      if record.coverage is not None]
    coverage = {
        key: (sum(item[key] for item in coverage_known)
              if coverage_known else None)
        for key in COVERAGE_FIELDS
    }
    coverage_complete = len(coverage_known) == len(records)
    coverage_total = (sum(coverage[key] for key in COVERAGE_FIELDS)
                      if coverage_known else None)
    coverage_rates = {
        f"{key}_per_mille": (
            _permille(coverage[key], coverage_total)
            if coverage_complete and coverage_total else None)
        for key in COVERAGE_FIELDS
    }
    predicted = [record.critical_path.get("predicted")
                 for record in records]
    observed = [record.critical_path.get("observed")
                for record in records]
    pairs = [(p, o) for p, o in zip(predicted, observed)
             if p is not None and o is not None]
    critical = {
        "predicted_ms": sum(p for p, _ in pairs) if pairs else None,
        "observed_ms": sum(o for _, o in pairs) if pairs else None,
        "delta_ms": sum(o - p for p, o in pairs) if pairs else None,
        "mean_absolute_error_ms": _mean(
            sum(abs(o - p) for p, o in pairs), len(pairs)),
        "pairs": len(pairs),
        "unknown_pairs": len(records) - len(pairs),
    }
    prefetch = {}
    for field_name in PREFETCH_FIELDS:
        prefetch[field_name] = _accumulate(
            [record.prefetch.get(field_name)
             if record.prefetch is not None else None
             for record in records])
    net_values = []
    for record in records:
        data = record.prefetch or {}
        if data.get("estimated_saved_ms") is not None \
                and data.get("overhead_ms") is not None:
            net_values.append(data["estimated_saved_ms"]
                              - data["overhead_ms"])
        else:
            net_values.append(None)
    prefetch["net_estimated_ms"] = _accumulate(net_values)
    prefetch["use_per_mille"] = (
        _permille(prefetch["used"]["total"], prefetch["hits"]["total"])
        if prefetch["used"]["total"] is not None
        and prefetch["hits"]["total"] is not None
        and prefetch["used"]["unknown_runs"] == 0
        and prefetch["hits"]["unknown_runs"] == 0 else None)
    return {
        "arm": arm,
        "runs": len(records),
        "verified_successes": successes,
        "valid_runs": valid_runs,
        "invalid_runs": invalid_runs,
        "unknown_validity_runs": unknown_validity,
        "decisive_runs": len(decisive),
        "unknown_check_runs": unknown_check_runs,
        "invalid_check_runs": invalid_check_runs,
        "success_rate_per_mille": _permille(successes, len(decisive)),
        "check_counts": check_counts,
        "time_to_verified": {
            "total_ms": time_total,
            "known_runs": len(known_times),
            "unknown_runs": len(verified_times) - len(known_times),
            "mean_ms": _mean(time_total, len(known_times)),
        },
        "verified_tasks_per_hour_milli": _rate_per_hour_milli(
            successes, time_total),
        "wall": wall,
        "timing": timing,
        "tool_calls": tool_calls,
        "tokens": {**tokens, "total": token_total},
        "cost_micros": cost,
        "parse": {
            "attempts": _out_accumulated(attempts),
            "failures": _out_accumulated(failures),
            "failure_per_mille": parse_rate,
        },
        "fallbacks": {
            "total": fallbacks["total"],
            "known_runs": fallbacks["known_runs"],
            "unknown_runs": fallbacks["unknown_runs"],
            "runs_with_fallbacks": fallback_runs,
            "run_per_mille": fallback_rate,
        },
        "coverage": {
            **{key: coverage[key] for key in COVERAGE_FIELDS},
            "known_runs": len(coverage_known),
            "unknown_runs": len(records) - len(coverage_known),
            "complete": coverage_complete,
            **coverage_rates,
        },
        "critical_path": critical,
        "prefetch": prefetch,
    }


def _public_prefetch(prefetch: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (*PREFETCH_FIELDS, "net_estimated_ms"):
        row = prefetch.get(key)
        if isinstance(row, Mapping):
            out[key] = {
                "total": _out(row.get("total")),
                "known_runs": row.get("known_runs", 0),
                "unknown_runs": row.get("unknown_runs", 0),
            }
        else:
            out[key] = {"total": _out(row), "known_runs": 0,
                        "unknown_runs": 0}
    out["use_per_mille"] = _out(prefetch.get("use_per_mille"))
    return out


def _public_arm(metrics: Mapping[str, Any]) -> dict[str, Any]:
    timing = {
        f"{field_name}_ms": {
            "total": _out(row.get("total")),
            "known_runs": row.get("known_runs", 0),
            "unknown_runs": row.get("unknown_runs", 0),
        }
        for field_name, row in metrics["timing"].items()
    }
    tool_row = metrics["tool_calls"]
    time_row = metrics["time_to_verified"]
    coverage = {
        key: _out(metrics["coverage"][key]) for key in COVERAGE_FIELDS
    }
    coverage.update({
        "known_runs": metrics["coverage"]["known_runs"],
        "unknown_runs": metrics["coverage"]["unknown_runs"],
        "complete": metrics["coverage"]["complete"],
        **{f"{key}_per_mille": _out(metrics["coverage"][f"{key}_per_mille"])
           for key in COVERAGE_FIELDS},
    })
    critical = {
        key: _out(value) if key not in ("pairs", "unknown_pairs") else value
        for key, value in metrics["critical_path"].items()
    }
    return {
        "arm": metrics["arm"],
        "runs": metrics["runs"],
        "verified_successes": metrics["verified_successes"],
        "valid_runs": metrics["valid_runs"],
        "invalid_runs": metrics["invalid_runs"],
        "unknown_validity_runs": metrics["unknown_validity_runs"],
        "decisive_runs": metrics["decisive_runs"],
        "unknown_check_runs": metrics["unknown_check_runs"],
        "invalid_check_runs": metrics["invalid_check_runs"],
        "success_rate_per_mille": _out(metrics["success_rate_per_mille"]),
        "check_counts": metrics["check_counts"],
        "time_to_verified": {
            "total_ms": _out(time_row["total_ms"]),
            "known_runs": time_row["known_runs"],
            "unknown_runs": time_row["unknown_runs"],
            "mean_ms": _out(time_row["mean_ms"]),
        },
        "verified_tasks_per_hour_milli": _out(
            metrics["verified_tasks_per_hour_milli"]),
        "wall_ms": {
            "total": _out(metrics["wall"].get("total")),
            "known_runs": metrics["wall"].get("known_runs", 0),
            "unknown_runs": metrics["wall"].get("unknown_runs", 0),
        },
        "timing_ms": timing,
        "tool_calls": {
            "total": _out(tool_row.get("total")),
            "known_runs": tool_row.get("known_runs", 0),
            "unknown_runs": tool_row.get("unknown_runs", 0),
            "mean_milli": _out(tool_row.get("mean_milli")),
        },
        "tokens": {
            **{
                key.removeprefix("tokens_"): {
                    "total": _out(row.get("total")),
                    "known_runs": row.get("known_runs", 0),
                    "unknown_runs": row.get("unknown_runs", 0),
                }
                for key, row in metrics["tokens"].items()
                if key.startswith("tokens_")
            },
            "total": {
                "total": _out(metrics["tokens"]["total"].get("total")),
                "known_runs": metrics["tokens"]["total"].get("known_runs", 0),
                "unknown_runs": metrics["tokens"]["total"].get(
                    "unknown_runs", 0),
            },
        },
        "cost_micros": {
            "total": _out(metrics["cost_micros"].get("total")),
            "known_runs": metrics["cost_micros"].get("known_runs", 0),
            "unknown_runs": metrics["cost_micros"].get("unknown_runs", 0),
        },
        "parse": {
            **metrics["parse"],
            "attempts": {
                key: _out(value) for key, value in
                metrics["parse"]["attempts"].items()
            },
            "failures": {
                key: _out(value) for key, value in
                metrics["parse"]["failures"].items()
            },
            "failure_per_mille": _out(metrics["parse"]["failure_per_mille"]),
        },
        "fallbacks": {
            **{key: value if key in ("known_runs", "unknown_runs")
               else _out(value)
               for key, value in metrics["fallbacks"].items()},
        },
        "coverage": coverage,
        "critical_path": critical,
        "prefetch": _public_prefetch(metrics["prefetch"]),
    }


def _delta_rows(
    metrics: Mapping[str, Mapping[str, Any]],
    arms: Iterable[str],
    reference: str,
) -> list[dict[str, Any]]:
    ref = metrics[reference]
    rows = []
    for arm in arms:
        if arm == reference:
            continue
        row = metrics[arm]
        ref_time = ref["time_to_verified"]["mean_ms"]
        arm_time = row["time_to_verified"]["mean_ms"]
        rows.append({
            "arm": arm,
            "reference": reference,
            "success_rate_delta_per_mille": _out(
                None if ref["success_rate_per_mille"] is None
                or row["success_rate_per_mille"] is None
                else row["success_rate_per_mille"]
                - ref["success_rate_per_mille"]),
            "time_to_verified_mean_delta_ms": _out(
                None if ref_time is None or arm_time is None
                else arm_time - ref_time),
            "verified_tasks_per_hour_milli_delta": _out(
                None if ref["verified_tasks_per_hour_milli"] is None
                or row["verified_tasks_per_hour_milli"] is None
                else row["verified_tasks_per_hour_milli"]
                - ref["verified_tasks_per_hour_milli"]),
            "cost_micros_delta": _out(
                None if ref["cost_micros"]["total"] is None
                or row["cost_micros"]["total"] is None
                else row["cost_micros"]["total"]
                - ref["cost_micros"]["total"]),
            "tool_calls_delta": _out(
                None if ref["tool_calls"]["total"] is None
                or row["tool_calls"]["total"] is None
                else row["tool_calls"]["total"]
                - ref["tool_calls"]["total"]),
            "tokens_total_delta": _out(
                None if ref["tokens"]["total"]["total"] is None
                or row["tokens"]["total"]["total"] is None
                else row["tokens"]["total"]["total"]
                - ref["tokens"]["total"]["total"]),
        })
    return rows


def _paired_prefetch(
    records: Iterable[ComparisonRecord],
    arms: Iterable[str],
) -> dict[str, Any]:
    arms = set(arms)
    grouped: dict[tuple[str, str], dict[bool, list[ComparisonRecord]]] = {}
    for record in records:
        if record.arm not in arms or record.prefetch_enabled is None:
            continue
        grouped.setdefault((record.task, record.arm), {}).setdefault(
            record.prefetch_enabled, []).append(record)
    pairs = []
    for (task, arm), states in sorted(grouped.items()):
        if True not in states or False not in states:
            continue
        on = states[True]
        off = states[False]
        on_compat = all(record.acceptance == on[0].acceptance
                        and record.revision == on[0].revision
                        and record.model == on[0].model
                        and record.permissions == on[0].permissions
                        and record.check == on[0].check for record in on)
        off_compat = all(record.acceptance == off[0].acceptance
                         and record.revision == off[0].revision
                         and record.model == off[0].model
                         and record.permissions == off[0].permissions
                         and record.check == off[0].check for record in off)
        pair_compat = on_compat and off_compat and all(
            getattr(on[0], dimension) is not None
            and getattr(on[0], dimension) == getattr(off[0], dimension)
            for dimension in PAIR_DIMENSIONS)
        on_times = [record.time_to_verified_ms for record in on
                    if _run_verified(record)
                    and record.time_to_verified_ms is not None]
        off_times = [record.time_to_verified_ms for record in off
                     if _run_verified(record)
                     and record.time_to_verified_ms is not None]
        on_mean = _mean(sum(on_times), len(on_times)) if on_times else None
        off_mean = _mean(sum(off_times), len(off_times)) if off_times else None
        pairs.append({
            "task": task,
            "arm": arm,
            "on_runs": len(on),
            "off_runs": len(off),
            "on_verified_with_time": len(on_times),
            "off_verified_with_time": len(off_times),
            "compatible": pair_compat,
            "on_mean_ms": _out(on_mean),
            "off_mean_ms": _out(off_mean),
            "delta_ms": _out(
                None if on_mean is None or off_mean is None
                else on_mean - off_mean),
        })
    comparable = [pair for pair in pairs
                  if pair["compatible"] and pair["delta_ms"] != "unknown"]
    if not pairs:
        return {
            "status": "not_paired",
            "pairs": [],
            "faster_on": 0,
            "faster_off": 0,
            "equal": 0,
            "unknown": 0,
            "total_delta_ms": "unknown",
        }
    faster_on = sum(1 for pair in comparable if pair["delta_ms"] < 0)
    faster_off = sum(1 for pair in comparable if pair["delta_ms"] > 0)
    equal = sum(1 for pair in comparable if pair["delta_ms"] == 0)
    total_delta = (sum(pair["delta_ms"] for pair in comparable)
                   if comparable else None)
    return {
        "status": "paired" if comparable else "unknown",
        "pairs": pairs,
        "faster_on": faster_on,
        "faster_off": faster_off,
        "equal": equal,
        "unknown": len(pairs) - len(comparable),
        "total_delta_ms": _out(total_delta),
    }


def _paired_time(
    complete_cells: Mapping[tuple[str, int], Mapping[str, ComparisonRecord]],
    reference: str,
    candidate: str,
) -> dict[str, Any]:
    ref_total = 0
    cand_total = 0
    pairs = 0
    unknown = 0
    for cell in sorted(complete_cells):
        ref = complete_cells[cell].get(reference)
        other = complete_cells[cell].get(candidate)
        if ref is None or other is None:
            continue
        if not (_run_verified(ref) and _run_verified(other)):
            unknown += 1
            continue
        if ref.time_to_verified_ms is None \
                or other.time_to_verified_ms is None:
            unknown += 1
            continue
        ref_total += ref.time_to_verified_ms
        cand_total += other.time_to_verified_ms
        pairs += 1
    improvement = None
    if pairs and ref_total > 0:
        improvement = ((ref_total - cand_total) * 1000
                       + ref_total // 2) // ref_total
    return {
        "reference": reference,
        "candidate": candidate,
        "pairs": pairs,
        "unknown_pairs": unknown,
        "reference_total_ms": _out(ref_total if pairs else None),
        "candidate_total_ms": _out(cand_total if pairs else None),
        "delta_ms": _out(cand_total - ref_total if pairs else None),
        "candidate_improvement_per_mille": _out(improvement),
    }


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def _normalize_thresholds(
    thresholds: Mapping[str, Any] | None,
    aliases: Mapping[str, str],
    compare_arms: Iterable[str],
) -> dict[str, Any] | None:
    if thresholds is None:
        return None
    if not isinstance(thresholds, Mapping):
        raise ComparisonError("thresholds must be a mapping",
                              code="compare.invalid_thresholds")
    allowed = {
        "reference_arm", "candidate_arm", "min_verified_runs",
        "max_success_drop_per_mille",
        "min_time_to_verified_improvement_per_mille",
        "max_prefetch_overhead_ms",
    }
    unknown = [key for key in thresholds if key not in allowed]
    if unknown:
        raise ComparisonError(f"unknown threshold fields: {sorted(unknown)}",
                              code="compare.invalid_thresholds")
    required = {"reference_arm", "candidate_arm", "min_verified_runs",
                "max_success_drop_per_mille",
                "min_time_to_verified_improvement_per_mille"}
    missing = sorted(required - set(thresholds))
    if missing:
        raise ComparisonError(f"thresholds are missing {missing}",
                              code="compare.invalid_thresholds")
    try:
        reference = _resolve_arm(thresholds["reference_arm"], aliases)
        candidate = _resolve_arm(thresholds["candidate_arm"], aliases)
    except ComparisonError as exc:
        raise ComparisonError(
            f"threshold arm is not in the arm vocabulary: {exc}",
            code="compare.invalid_thresholds") from exc
    arms = set(compare_arms)
    if reference not in arms or candidate not in arms:
        raise ComparisonError(
            "threshold arms must be part of the compared arms",
            code="compare.invalid_thresholds")
    if reference == candidate:
        raise ComparisonError("threshold arms must differ",
                              code="compare.invalid_thresholds")

    def required_int(name: str, *, minimum: int = 0) -> int:
        try:
            value = _int_or_none(thresholds[name], f"thresholds.{name}",
                                 minimum=minimum)
        except ComparisonError as exc:
            raise ComparisonError(
                f"thresholds.{name} must be a known integer",
                code="compare.invalid_thresholds") from exc
        if value is None:
            raise ComparisonError(
                f"thresholds.{name} must be a known integer",
                code="compare.invalid_thresholds")
        return value

    normalized = {
        "reference_arm": reference,
        "candidate_arm": candidate,
        "min_verified_runs": required_int("min_verified_runs", minimum=1),
        "max_success_drop_per_mille": required_int(
            "max_success_drop_per_mille"),
        "min_time_to_verified_improvement_per_mille": required_int(
            "min_time_to_verified_improvement_per_mille"),
    }
    if "max_prefetch_overhead_ms" in thresholds:
        normalized["max_prefetch_overhead_ms"] = required_int(
            "max_prefetch_overhead_ms")
    return normalized


def _evaluate_thresholds(
    thresholds: Mapping[str, Any] | None,
    *,
    status: str,
    metrics: Mapping[str, Mapping[str, Any]] | None,
    paired_time: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if thresholds is None:
        return {
            "status": "not_evaluated",
            "reason": "thresholds_unresolved",
            "checks": [],
        }
    if metrics is None or status not in ("compatible", "partial"):
        return {
            "status": "not_evaluated",
            "reason": "compatible_aggregate_required",
            "checks": [],
        }
    reference = thresholds["reference_arm"]
    candidate = thresholds["candidate_arm"]
    ref = metrics[reference]
    arm = metrics[candidate]
    checks = []
    required = thresholds["min_verified_runs"]
    for label, row in ((reference, ref), (candidate, arm)):
        checks.append({
            "name": f"verified_runs:{label}",
            "status": "pass" if row["verified_successes"] >= required
            else "fail",
            "observed": row["verified_successes"],
            "threshold": required,
        })
    ref_rate = ref["success_rate_per_mille"]
    arm_rate = arm["success_rate_per_mille"]
    if ref_rate is None or arm_rate is None:
        checks.append({
            "name": "success_drop_per_mille",
            "status": "unknown",
            "observed": "unknown",
            "threshold": thresholds["max_success_drop_per_mille"],
        })
    else:
        drop = ref_rate - arm_rate
        checks.append({
            "name": "success_drop_per_mille",
            "status": "pass"
            if drop <= thresholds["max_success_drop_per_mille"] else "fail",
            "observed": drop,
            "threshold": thresholds["max_success_drop_per_mille"],
        })
    improvement = (paired_time or {}).get("candidate_improvement_per_mille")
    if improvement is None or improvement == "unknown":
        checks.append({
            "name": "time_to_verified_improvement_per_mille",
            "status": "unknown",
            "observed": "unknown",
            "threshold": thresholds[
                "min_time_to_verified_improvement_per_mille"],
        })
    else:
        checks.append({
            "name": "time_to_verified_improvement_per_mille",
            "status": "pass"
            if improvement >= thresholds[
                "min_time_to_verified_improvement_per_mille"] else "fail",
            "observed": improvement,
            "threshold": thresholds[
                "min_time_to_verified_improvement_per_mille"],
        })
    if "max_prefetch_overhead_ms" in thresholds:
        overhead = arm["prefetch"]["overhead_ms"]["total"]
        checks.append({
            "name": "prefetch_overhead_ms",
            "status": "unknown" if overhead is None
            else ("pass" if overhead <= thresholds["max_prefetch_overhead_ms"]
                  else "fail"),
            "observed": _out(overhead),
            "threshold": thresholds["max_prefetch_overhead_ms"],
        })
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        verdict = "fail"
        reason = "threshold_failed"
    elif "unknown" in statuses:
        verdict = "not_evaluated"
        reason = "unknown_measure"
    else:
        verdict = "pass"
        reason = "thresholds_met"
    return {"status": verdict, "reason": reason, "checks": checks}


# ---------------------------------------------------------------------------
# Document assembly, bounds and rendering
# ---------------------------------------------------------------------------


def _cell_summary(cells: Iterable[tuple[str, int]],
                  complete: Mapping[tuple[str, int], Any]) -> dict[str, int]:
    cells = list(cells)
    return {
        "total": len(cells),
        "complete": len(complete),
        "partial": sum(1 for cell in cells if cell not in complete),
        "tasks": len({cell[0] for cell in cells}),
    }


def _normalize_scope(scope: Mapping[str, Any] | None, record_count: int,
                     task_count: int) -> dict[str, Any]:
    if scope is None:
        return {"records": record_count, "tasks": task_count}
    if not isinstance(scope, Mapping):
        raise ComparisonError("scope must be a mapping",
                              code="compare.invalid_input")
    value = dict(scope)
    try:
        C.canonical_json(value)
    except C.ContractError as exc:
        raise ComparisonError(f"scope is not canonical JSON: {exc}",
                              code="compare.invalid_input") from exc
    return value


def _document_without_hash(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items()
            if key not in ("hash", "created_at")}


def comparison_hash(document: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical comparison without ``hash``/``created_at``."""
    if not isinstance(document, Mapping):
        raise ComparisonError("comparison must be a mapping",
                              code="compare.invalid_comparison")
    return C.content_hash(_document_without_hash(document))


def _trim(document: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    omissions = document["omissions"]
    while True:
        body = {key: value for key, value in document.items()
                if key != "hash"}
        probe = {**body, "hash": "0" * 64}
        if len(C.canonical_json(probe).encode("utf-8")) <= max_bytes:
            break
        if document["runs"]:
            document["runs"].pop()
            omissions["omitted_details"] += 1
            omissions["truncated"] = True
            omissions["reason"] = omissions["reason"] or "bytes"
            omissions["next_cursor"] = (
                document["runs"][-1]["id"] if document["runs"] else None)
            continue
        if document["compatibility"]["cells"]:
            document["compatibility"]["cells"].pop()
            document["compatibility"]["cells_omitted"] += 1
            omissions["omitted_compatibility_cells"] += 1
            omissions["truncated"] = True
            omissions["reason"] = omissions["reason"] or "bytes"
            continue
        if document["not_assessed"]:
            document["not_assessed"].pop()
            omissions["omitted_not_assessed"] += 1
            omissions["truncated"] = True
            omissions["reason"] = omissions["reason"] or "bytes"
            continue
        raise ComparisonError(
            f"comparison cannot fit in {max_bytes} bytes after trimming",
            code="compare.bounds_exceeded")
    if omissions["truncated"] and omissions["reason"] is None:
        omissions["reason"] = "bytes"
    return document


def _validate_bounds(max_records: int, max_bytes: int) -> None:
    if isinstance(max_records, bool) or not isinstance(max_records, int) \
            or max_records < 0:
        raise ComparisonError("max_records must be a non-negative integer",
                              code="compare.invalid_input")
    if max_records > HARD_MAX_RECORDS:
        raise ComparisonError(
            f"max_records exceeds the hard cap {HARD_MAX_RECORDS}",
            code="compare.bounds_exceeded")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or max_bytes < 1024:
        raise ComparisonError("max_bytes must be an integer of at least 1024",
                              code="compare.invalid_input")
    if max_bytes > HARD_MAX_BYTES:
        raise ComparisonError(
            f"max_bytes exceeds the hard cap {HARD_MAX_BYTES}",
            code="compare.bounds_exceeded")


def build_comparison(
    records: Any,
    *,
    arms: Iterable[str] | None = None,
    required_arms: Iterable[str] | None = None,
    aliases: Mapping[str, str] | None = None,
    defaults: Mapping[str, Any] | None = None,
    reference_arm: str | None = None,
    thresholds: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    created_at: int | str | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Build the ``ozagia.eval_comparison`` v1.0 document.

    ``records`` is a list of explicit records, a JSON object with a ``runs``
    list, a single record object, or JSON text for any of those.  No file,
    network, model or daemon operation occurs.  A comparison is only
    aggregated when every compared cell is compatible; see the module
    docstring and ``docs/eval_compare.md``.
    """
    _validate_bounds(max_records, max_bytes)
    resolved_aliases = _arm_aliases(aliases)
    normalized = _ingest(records, defaults=defaults,
                         aliases=resolved_aliases)
    normalized.sort(key=_record_sort_key)
    runs_total = len(normalized)
    if runs_total > HARD_MAX_RECORDS:
        raise ComparisonError(
            f"{runs_total} records exceed the hard cap {HARD_MAX_RECORDS}",
            code="compare.bounds_exceeded")
    omitted_runs = 0
    if runs_total > max_records:
        omitted_runs = runs_total - max_records
        normalized = normalized[:max_records]
    seen: set[tuple[str, int, str]] = set()
    for record in normalized:
        key = (record.task, record.repeat, record.arm)
        if key in seen:
            raise ComparisonError(
                f"duplicate run for task {record.task!r}, repeat "
                f"{record.repeat}, arm {record.arm!r}",
                code="compare.duplicate_run")
        seen.add(key)

    present_arms = {record.arm for record in normalized}
    if arms is None:
        compare_arms = [arm for arm in ARM_ORDER if arm in present_arms]
    else:
        compare_arms = []
        for arm in arms:
            canonical = _resolve_arm(arm, resolved_aliases)
            if canonical not in compare_arms:
                compare_arms.append(canonical)
        compare_arms.sort(key=lambda arm: ARM_INDEX[arm])
    if required_arms is None:
        required = list(compare_arms)
    else:
        required = []
        for arm in required_arms:
            canonical = _resolve_arm(arm, resolved_aliases)
            if arms is not None and canonical not in compare_arms:
                raise ComparisonError(
                    f"required arm {canonical!r} is not in the compared arms",
                    code="compare.invalid_input")
            if canonical not in required:
                required.append(canonical)
        required.sort(key=lambda arm: ARM_INDEX[arm])
    if reference_arm is None:
        reference = (compare_arms[0] if compare_arms
                     else DEFAULT_REFERENCE_ARM)
    else:
        reference = _resolve_arm(reference_arm, resolved_aliases)
    if compare_arms and reference not in compare_arms:
        raise ComparisonError(
            f"reference arm {reference!r} is not in the compared arms",
            code="compare.invalid_input")
    normalized_thresholds = _normalize_thresholds(
        thresholds, resolved_aliases, compare_arms)

    by_cell: dict[tuple[str, int], dict[str, ComparisonRecord]] = {}
    for record in normalized:
        if record.arm not in compare_arms:
            continue
        by_cell.setdefault((record.task, record.repeat), {})[record.arm] = record
    all_cells = sorted(by_cell)
    complete_cells = {
        cell: arms_in_cell for cell, arms_in_cell in by_cell.items()
        if all(arm in arms_in_cell for arm in compare_arms)
    }
    cell_rows = [_cell_compatibility(cell, complete_cells[cell].values(),
                                     compare_arms)
                 for cell in sorted(complete_cells)]
    set_rows = _set_compatibility(complete_cells) if complete_cells else {}
    issues = _compatibility_issues(cell_rows, set_rows) if complete_cells else []
    compatibility_reasons = _compatibility_reasons(issues)
    has_mismatch = any(issue["status"] == "mismatch" for issue in issues)
    has_unknown = any(issue["status"] == "unknown" for issue in issues)

    reasons: list[str] = []
    if not normalized:
        status = "empty"
        reasons.append("no_records")
    elif len(compare_arms) < 2:
        status = "incomplete"
        reasons.append("less_than_two_arms")
    elif any(arm not in compare_arms for arm in required):
        status = "incomplete"
        reasons.append("missing_required_arms")
    elif not complete_cells:
        status = "unpaired"
        reasons.append("no_cell_has_every_arm")
    elif has_mismatch:
        status = "incompatible"
        reasons.append("compatibility_mismatch")
    elif has_unknown:
        status = "incompatible"
        reasons.append("compatibility_unknown")
    elif len(complete_cells) < len(all_cells):
        status = "partial"
        reasons.append("unpaired_cells")
    else:
        status = "compatible"
        reasons.append("matched_cells")
    if compatibility_reasons:
        reasons.extend(reason for reason in compatibility_reasons
                       if reason not in reasons)

    metrics: dict[str, dict[str, Any]] | None = None
    if status in ("compatible", "partial") and complete_cells:
        metrics = {
            arm: _arm_metrics(
                arm,
                [complete_cells[cell][arm] for cell in sorted(complete_cells)])
            for arm in compare_arms
        }
    paired_time = None
    if metrics is not None and compare_arms:
        if normalized_thresholds is not None:
            paired_reference = normalized_thresholds["reference_arm"]
            paired_candidate = normalized_thresholds["candidate_arm"]
        else:
            paired_reference = reference
            paired_candidate = next(arm for arm in compare_arms
                                    if arm != reference)
        paired_time = _paired_time(complete_cells, paired_reference,
                                   paired_candidate)
    aggregate = None
    if metrics is not None:
        aggregate = {
            "arms": [_public_arm(metrics[arm]) for arm in compare_arms],
            "deltas": _delta_rows(metrics, compare_arms, reference),
            "paired_time": paired_time,
            "prefetch_pairing": _paired_prefetch(normalized, compare_arms),
            "measures": [
                "task_success_and_validity", "time_to_verified_outcome",
                "verified_tasks_per_hour", "wall_clock_components",
                "tool_calls", "tokens", "cost", "parse_and_fallback",
                "stale_and_unknown_coverage", "predicted_vs_observed_critical_path",
                "prefetch_hit_use_waste_and_overhead",
            ],
        }
    verdict = _evaluate_thresholds(
        normalized_thresholds, status=status, metrics=metrics,
        paired_time=paired_time)

    detail_runs = []
    for record in normalized:
        row = record.to_dict()
        row["included"] = record.arm in compare_arms
        detail_runs.append(row)
    document: dict[str, Any] = {
        "schema": {"namespace": COMPARISON_NAMESPACE,
                   "version": COMPARISON_VERSION},
        "comparison_version": COMPARISON_VERSION,
        "scope": _normalize_scope(scope, len(normalized),
                                  len({r.task for r in normalized})),
        "status": status,
        "compatible": status in ("compatible", "partial"),
        "reasons": reasons,
        "arms": compare_arms,
        "required_arms": required,
        "compatibility": {
            "rules": {
                "cell_dimensions": list(COMPATIBILITY_DIMENSIONS),
                "set_dimensions": list(SET_DIMENSIONS),
                "pairing": "task_and_repeat",
                "unknown_policy": "unknown_is_incompatible",
                "self_report_policy": "never_acceptance",
            },
            "cells": cell_rows,
            "cells_omitted": 0,
            "set": set_rows,
            "issues": issues,
            "reasons": compatibility_reasons,
        },
        "cells": _cell_summary(all_cells, complete_cells),
        "aggregate": aggregate,
        "runs": detail_runs,
        "bounds": {
            "max_records": max_records,
            "hard_max_records": HARD_MAX_RECORDS,
            "max_bytes": max_bytes,
            "hard_max_bytes": HARD_MAX_BYTES,
            "max_arms": MAX_ARMS,
            "runs_total": runs_total,
            "runs_included": sum(1 for r in normalized
                                 if r.arm in compare_arms),
            "cells_total": len(all_cells),
            "cells_complete": len(complete_cells),
        },
        "omissions": {
            "truncated": omitted_runs > 0,
            "omitted_runs": omitted_runs,
            "omitted_details": 0,
            "omitted_compatibility_cells": 0,
            "omitted_not_assessed": 0,
            "next_cursor": (normalized[-1].id
                            if omitted_runs and normalized else None),
            "reason": "limit" if omitted_runs else None,
        },
        "not_assessed": [
            "live model, driver, network, daemon and target-project execution",
            "model self-reported correctness as an acceptance result",
            "operator preparation where a record does not measure it",
            "acceptance-criteria digest verification against a task file",
            "cost-currency normalization; cost is integer micro-units as supplied",
            "wall-clock inference from timestamp order; durations are supplied",
        ],
        "thresholds": normalized_thresholds,
        "verdict": verdict,
    }
    if created_at is not None:
        if isinstance(created_at, bool) or not isinstance(created_at, (int, str)):
            raise ComparisonError("created_at must be an integer or string",
                                  code="compare.invalid_input")
        document["created_at"] = created_at
    document = _trim(document, max_bytes)
    document["hash"] = comparison_hash(document)
    return validate_comparison(document)


def validate_comparison(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the v1.0 comparison, its canonical form and its hash."""
    if not isinstance(document, Mapping):
        raise ComparisonError("comparison must be a mapping",
                              code="compare.invalid_comparison")
    normalized = dict(document)
    schema = normalized.get("schema")
    if not isinstance(schema, Mapping) \
            or schema.get("namespace") != COMPARISON_NAMESPACE:
        raise ComparisonError(
            f"comparison schema namespace must be {COMPARISON_NAMESPACE!r}",
            code="compare.invalid_comparison")
    if schema.get("version") != COMPARISON_VERSION \
            or normalized.get("comparison_version") != COMPARISON_VERSION:
        raise ComparisonError(
            f"comparison version must be {COMPARISON_VERSION!r}",
            code="compare.invalid_comparison")
    missing = [key for key in COMPARISON_SCHEMA["required"]
               if key not in normalized]
    if missing:
        raise ComparisonError(
            f"comparison is missing required fields: {missing}",
            code="compare.invalid_comparison")
    allowed = set(COMPARISON_SCHEMA["types"]["comparison"]["fields"])
    unknown = [key for key in normalized
               if key not in allowed and not str(key).startswith("x-")]
    if unknown:
        raise ComparisonError(
            f"comparison has unknown fields: {sorted(unknown)}",
            code="compare.invalid_comparison")
    if normalized.get("status") not in COMPARISON_STATUSES:
        raise ComparisonError("comparison status is invalid",
                              code="compare.invalid_comparison")
    if normalized.get("compatible") is not True \
            and normalized.get("compatible") is not False:
        raise ComparisonError("comparison compatible must be a bool",
                              code="compare.invalid_comparison")
    verdict = normalized.get("verdict")
    if not isinstance(verdict, Mapping) \
            or verdict.get("status") not in VERDICT_STATUSES:
        raise ComparisonError("comparison verdict status is invalid",
                              code="compare.invalid_comparison")
    if normalized["status"] in ("empty", "incomplete", "unpaired",
                                "incompatible") \
            and normalized.get("aggregate") is not None:
        raise ComparisonError(
            "an incompatible or unpaired comparison must not carry an "
            "aggregate", code="compare.invalid_comparison")
    if scored_status(normalized) and normalized.get("aggregate") is None:
        raise ComparisonError(
            "a compatible or partial comparison must carry an aggregate",
            code="compare.invalid_comparison")
    if scored_status(normalized) and normalized.get("compatible") is not True:
        raise ComparisonError(
            "a compatible or partial comparison must set compatible=true",
            code="compare.invalid_comparison")
    if not scored_status(normalized) and normalized.get("compatible") is not False:
        raise ComparisonError(
            "an incompatible or unpaired comparison must set compatible=false",
            code="compare.invalid_comparison")
    try:
        C.canonical_json(normalized)
    except C.ContractError as exc:
        raise ComparisonError(
            f"comparison is not canonical JSON: {exc}",
            code="compare.invalid_comparison") from exc
    expected = comparison_hash(normalized)
    if normalized.get("hash") != expected:
        raise ComparisonError("comparison hash does not match its content",
                              code="compare.invalid_comparison")
    return normalized


def scored_status(document: Mapping[str, Any]) -> bool:
    """Whether the document claims a compatible aggregate."""
    return document.get("status") in ("compatible", "partial")


def comparison_json(document: Mapping[str, Any]) -> str:
    """Return a validated comparison as canonical JSON."""
    return C.canonical_json(validate_comparison(document))


def comparison_schema() -> dict[str, Any]:
    """Return the normalized v1.0 comparison schema record."""
    return C.validate_schema(COMPARISON_SCHEMA)


def comparison_schema_fingerprint() -> str:
    """Return the SHA-256 schema fingerprint for the comparison schema."""
    return C.schema_fingerprint(COMPARISON_SCHEMA)


def _permille_text(value: Any) -> str:
    if value == "unknown" or value is None:
        return "unknown"
    sign = "-" if value < 0 else ""
    value = abs(int(value))
    return f"{sign}{value // 1000}.{value % 1000:03d}"


def _ms_text(value: Any) -> str:
    return "unknown" if value == "unknown" or value is None else f"{value} ms"


def format_comparison(document: Mapping[str, Any], *, max_lines: int = 200
                      ) -> str:
    """Render a bounded readable view of a comparison document."""
    document = validate_comparison(document)
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) \
            or max_lines < 1:
        raise ComparisonError("max_lines must be a positive integer",
                              code="compare.invalid_input")
    lines = [
        "eval comparison",
        f"  status: {document['status']} "
        f"(compatible={document['compatible']})",
        f"  arms: {', '.join(document['arms']) or 'none'}",
        f"  reasons: {', '.join(document['reasons'])}",
    ]
    if document["compatibility"]["issues"]:
        lines.append("  compatibility issues:")
        for issue in document["compatibility"]["issues"][:20]:
            lines.append(
                f"    {issue['scope']} {issue.get('task')} "
                f"repeat {issue.get('repeat')} {issue['dimension']}: "
                f"{issue['status']}")
    aggregate = document["aggregate"]
    if aggregate is None:
        lines.append("  aggregate: refused (no compatible matched cells)")
    else:
        lines.append(
            "  arm            runs verified  valid success/1000 "
            "mean-verified       tasks/hour")
        for row in aggregate["arms"]:
            lines.append(
                f"  {row['arm']:<14} {row['runs']:>4} "
                f"{row['verified_successes']:>8} {row['valid_runs']:>6} "
                f"{_permille_text(row['success_rate_per_mille']):>12} "
                f"{_ms_text(row['time_to_verified']['mean_ms']):>16} "
                f"{_permille_text(row['verified_tasks_per_hour_milli']):>14}")
        lines.append("  deltas:")
        for row in aggregate["deltas"]:
            lines.append(
                f"    {row['arm']} vs {row['reference']}: success "
                f"{_permille_text(row['success_rate_delta_per_mille'])}/1000, "
                f"time {_ms_text(row['time_to_verified_mean_delta_ms'])}, "
                f"throughput "
                f"{_permille_text(row['verified_tasks_per_hour_milli_delta'])}")
        lines.append("  measures:")
        for row in aggregate["arms"]:
            critical = row["critical_path"]
            prefetch = row["prefetch"]
            lines.append(
                f"    {row['arm']}: tool calls "
                f"{row['tool_calls']['total']}, tokens "
                f"{row['tokens']['total']['total']}, cost "
                f"{row['cost_micros']['total']} micros, parse failures "
                f"{_permille_text(row['parse']['failure_per_mille'])}/1000, "
                f"coverage current "
                f"{_permille_text(row['coverage']['current_per_mille'])}/1000, "
                f"critical-path pairs {critical['pairs']} "
                f"(unknown {critical['unknown_pairs']}), prefetch overhead "
                f"{_ms_text(prefetch['overhead_ms']['total'])}")
            lines.append(
                f"      wall {_ms_text(row['wall_ms']['total'])}, "
                f"model {_ms_text(row['timing_ms']['model_ms']['total'])}, "
                f"tools {_ms_text(row['timing_ms']['tool_ms']['total'])}, "
                f"check {_ms_text(row['timing_ms']['check_ms']['total'])}")
        paired = aggregate.get("paired_time") or {}
        if paired:
            lines.append(
                f"  paired time {paired['reference']} -> "
                f"{paired['candidate']}: {paired['pairs']} pair(s), "
                f"improvement "
                f"{_permille_text(paired['candidate_improvement_per_mille'])}"
                "/1000")
        pairing = aggregate.get("prefetch_pairing") or {}
        lines.append(
            f"  prefetch pairing: {pairing.get('status')} "
            f"(on {pairing.get('faster_on')}, off "
            f"{pairing.get('faster_off')}, unknown "
            f"{pairing.get('unknown')})")
        if pairing.get("total_delta_ms") not in (None, "unknown"):
            lines.append(f"    total delta (on - off): "
                         f"{_ms_text(pairing.get('total_delta_ms'))}")
    verdict = document["verdict"]
    lines.append(f"  verdict: {verdict['status']} ({verdict['reason']})")
    for check in verdict.get("checks", [])[:10]:
        lines.append(
            f"    {check['name']}: {check['status']} "
            f"(observed {check['observed']}, threshold {check['threshold']})")
    omissions = document["omissions"]
    if omissions["truncated"]:
        lines.append(
            f"  truncated: {omissions['reason']} "
            f"(runs {omissions['omitted_runs']}, details "
            f"{omissions['omitted_details']}, compatibility cells "
            f"{omissions['omitted_compatibility_cells']})")
    lines.append(f"  hash: {document['hash']}")
    return "\n".join(lines[:max_lines])


__all__ = [
    "ARM_ORDER", "ARM_INDEX", "CHECK_RESULTS", "COMPARISON_NAMESPACE",
    "COMPARISON_SCHEMA", "COMPARISON_STATUSES", "COMPARISON_VERSION",
    "COMPATIBILITY_DIMENSIONS", "ComparisonError", "ComparisonRecord",
    "COUNT_FIELDS", "COVERAGE_FIELDS", "DEFAULT_ARM_ALIASES",
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_RECORDS", "DEFAULT_REFERENCE_ARM",
    "HARD_MAX_BYTES", "HARD_MAX_RECORDS", "MAX_ARMS", "PREFETCH_FIELDS",
    "SELF_REPORT_VALUES", "SET_DIMENSIONS", "TIMING_FIELDS",
    "VERDICT_STATUSES",
    "build_comparison", "comparison_hash", "comparison_json",
    "comparison_schema", "comparison_schema_fingerprint",
    "format_comparison", "records_from_report", "scored_status",
    "validate_comparison",
]
