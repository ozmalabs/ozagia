"""First-stage, read-only evidence joining for goals, runs and tests.

The collector answers *what is recorded* without deciding whether the work is
correct.  It joins, from the existing owners:

* a goal's criteria and their last recorded results;
* task run manifests and their artifacts/output digests;
* the working-set test record and the recorded outcome per test key;
* interpreter/environment candidates.

It is deliberately read-only: it never writes the goal, task, session or graph
stores, never runs a test or a probe, never refreshes the graph and never
starts a daemon.  Durable evidence writes (idempotency keys, retention, crash
recovery, an evidence store) are **deferred** to a later package; ``deferred``
in the report names them.

Redaction and bounds
--------------------

Only metadata, hashes and redacted references are stored.  There are no
prompts, no free-text goal titles/objectives, no task summaries, no event
payloads, no task output values and no raw logs.  Task output and artifacts
are represented by a digest or a supplied SHA-256 only.  Free-text labels are
bounded and passed through a best-effort secret-pattern redactor; the primary
defence is the allowlist, not the pattern list.  Every list is sorted and
capped, and the report carries an explicit truncation envelope.

A requested run that is not on disk is ``absent`` with a reason; it is never
silently dropped and never treated as current.  An unreadable manifest is
``invalid`` and an incomplete run is ``incomplete``; neither is upgraded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core import contracts as C

SCHEMA_NAMESPACE = "ozagia.evidence"
SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"

EVIDENCE_KINDS = ("criterion", "environment", "goal", "run", "test")
STATUS_MAP = {
    "pass": "passed",
    "fail": "failed",
    "stale": "unknown",
    "unknown": "unknown",
    "unchecked": "not_assessed",
}

MAX_CRITERIA = 50
MAX_RUNS = 32
MAX_TEST_KEYS = 64
MAX_INTERPRETERS = 16
MAX_LABELS = 20
MAX_REPORT_BYTES = 256 * 1024
MAX_REASON = 300
MAX_REF = 512

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
TEST_KEY_RE = re.compile(r"test:[^\s]{1,299}\Z")

DEFERRED = (
    "durable evidence records",
    "idempotency keys",
    "retention and compaction",
    "crash and partial-run recovery",
    "writing to the goal, task, session or graph stores",
)

_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)
_SAFE_SHA = re.compile(r"^[0-9a-f]{12,64}$")


class EvidenceError(ValueError):
    """A malformed evidence request with a stable service error code."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        self.code = code if code in C.SERVICE_ERROR_CODES else "invalid_request"
        super().__init__(_line(message))


# ---------------------------------------------------------------------------
# Bounded helpers
# ---------------------------------------------------------------------------


def _line(value: Any, limit: int = MAX_REASON) -> str:
    return " ".join(str(value).split())[:limit]


def _text(value: Any, limit: int = MAX_REF) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    return text[:limit]


def redact_text(value: Any, limit: int = 200) -> str:
    """Bound and redact a free-text label (best effort; allowlist first)."""
    text = _text(value, limit * 2)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:limit].strip()


def _mtime(path: Path) -> int | str:
    try:
        return int(path.stat().st_mtime_ns) // 1_000_000
    except OSError:
        return "unknown"


def _observed(value: Any, fallback: int | str) -> int | str:
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str) and 0 < len(value) <= 64:
        return value
    return fallback


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number < 0 else int(number * 1000)
    return None


def _duration_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number < 0 else int(round(number * 1000))
    return None


def _sha(value: Any) -> str | None:
    """Canonical content hash of an owner value, or None when not canonical."""
    try:
        return C.content_hash(value)
    except C.ContractError:
        return None


def _safe_sha(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_SHA.fullmatch(value):
        return value
    return None


def _goal_identity(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise EvidenceError("a goal id is required")
    value = text.strip()
    try:
        if value.startswith("goal:"):
            parsed = C.parse_id(value)
            return parsed.canonical, parsed.components[0]
        canonical = C.format_id("goal", value)
    except C.ContractError as exc:
        raise EvidenceError(f"invalid goal id {text!r}: {exc}") from exc
    return canonical, C.parse_id(canonical).components[0]


def _check_bound(value: Any, name: str, hard: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"{name} must be an integer")
    if value < 1 or value > hard:
        raise EvidenceError(f"{name} must be between 1 and {hard}")
    return value


def _run_ids(values: Iterable[str], maximum: int) -> tuple[list[str], int]:
    if isinstance(values, (str, bytes)):
        raise EvidenceError("run_ids must be an iterable of ids")
    checked: list[str] = []
    for value in values:
        if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
            raise EvidenceError(
                "run ids must match [A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
        checked.append(value)
    unique = sorted(dict.fromkeys(checked))
    return unique[:maximum], max(0, len(unique) - maximum)


def _test_keys(values: Iterable[str], maximum: int) -> tuple[list[str], int]:
    if isinstance(values, (str, bytes)):
        raise EvidenceError("test_keys must be an iterable of keys")
    checked: list[str] = []
    for value in values:
        if not isinstance(value, str) or not TEST_KEY_RE.fullmatch(value):
            raise EvidenceError("test keys must start with test: and be bounded")
        checked.append(value)
    unique = sorted(dict.fromkeys(checked))
    return unique[:maximum], max(0, len(unique) - maximum)


def _criterion_args(kind: str, args: Mapping[str, Any]) -> dict[str, str]:
    """Only graph-key/identifier arguments; never arbitrary values."""
    allowed = {
        "key", "caller", "callee", "finding_id", "attr", "packet",
    }
    out: dict[str, str] = {}
    for name in sorted(allowed):
        value = args.get(name)
        if not isinstance(value, str) or not value:
            continue
        if name in ("key", "caller", "callee") and not re.match(
                r"^(?:test|sym|file|ops):", value):
            continue
        out[name] = redact_text(value, 300)
    return out


def _test_record_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted part of the working-set test record for hashing."""
    runs = []
    for run in list(record.get("runs") or [])[-50:]:
        if not isinstance(run, Mapping):
            continue
        runs.append({
            "ts": _epoch_ms(run.get("ts")),
            "selectors": [_text(item, 300) for item in
                          (run.get("selectors") or [])[:50]],
            "failed": [_text(item, 300) for item in
                       (run.get("failed") or [])[:50]],
            "errors": [_text(item, 300) for item in
                       (run.get("errors") or [])[:50]],
            "complete": bool(run.get("complete")),
            "filtered": bool(run.get("filtered")),
            "source": _text(run.get("source"), 40),
        })
    tests = {}
    for key, value in sorted((record.get("tests") or {}).items()):
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        tests[_text(key, 300)] = {
            "ts": _epoch_ms(value.get("ts")),
            "outcome": _text(value.get("outcome"), 40),
        }
        if len(tests) >= 100:
            break
    return {"runs": runs, "tests": tests}


# ---------------------------------------------------------------------------
# Default read-only sources
# ---------------------------------------------------------------------------


def default_goal_source(root: Path, local_goal: str) -> dict[str, Any]:
    from ozagia.core import goals as GOALS

    path = GOALS.goals_path(root)
    observed_at = _mtime(path)
    if not path.is_file():
        return {"state": "absent", "path": path.as_posix(),
                "observed_at": observed_at,
                "reason": f"no goal store at {path}"}
    try:
        store = GOALS.load_store(root)
    except GOALS.GoalError as exc:
        return {"state": "invalid", "path": path.as_posix(),
                "observed_at": observed_at, "reason": _line(exc)}
    for goal in store.get("goals") or []:
        if goal.get("id") == local_goal:
            return {"state": "present", "goal": goal,
                    "path": path.as_posix(), "observed_at": observed_at}
    return {"state": "absent", "path": path.as_posix(),
            "observed_at": observed_at,
            "reason": f"no goal {local_goal!r} in {path}"}


def default_runs_source(root: Path,
                        run_ids: Sequence[str]) -> list[dict[str, Any]]:
    from ozagia.core import tasks as TASKS

    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            run = TASKS.show_run(root, run_id)
        except TASKS.TaskRunError as exc:
            text = _line(exc)
            state = "absent" if "no task run" in text else "invalid"
            rows.append({"run_id": run_id, "state": state, "reason": text,
                         "observed_at": "unknown"})
            continue
        run_dir = Path(str(run.get("run_dir") or ""))
        errors = [str(item) for item in run.get("errors") or []]
        if not errors and str(run.get("state")) == "incomplete":
            errors.append("the run is incomplete (no manifest)")
        rows.append({
            "run_id": run_id,
            "state": str(run.get("state") or "incomplete"),
            "run": run,
            "path": run_dir.as_posix() if run_dir else "unknown",
            "observed_at": _mtime(run_dir) if run_dir else "unknown",
            "reason": _line("; ".join(errors)) or None,
        })
    return rows


def default_test_record_source(root: Path) -> dict[str, Any]:
    from ozagia.config import state_path
    from ozagia.core import working_set as W

    path = state_path(root, *W.TEST_RECORD)
    observed_at = _mtime(path)
    if not path.is_file():
        return {"state": "absent", "path": path.as_posix(),
                "observed_at": observed_at,
                "reason": "no working-set test record"}
    try:
        record = W.load_test_record(root)
    except Exception as exc:  # noqa: BLE001 - unreadable is not absent
        return {"state": "invalid", "path": path.as_posix(),
                "observed_at": observed_at,
                "reason": _line(f"{type(exc).__name__}: {exc}")}
    return {"state": "present", "record": record,
            "path": path.as_posix(), "observed_at": observed_at}


def default_interpreters_source(root: Path) -> list[dict[str, Any]]:
    from ozagia.core import environments as ENV

    try:
        return list(ENV.discover(root, include_self=False, cache_mode="read",
                                 lock_tools=False))
    except Exception as exc:  # noqa: BLE001 - an unavailable source is visible
        return [{"state": "unavailable",
                 "reason": _line(f"{type(exc).__name__}: {exc}")}]


@dataclass(frozen=True)
class Sources:
    """Injection seams for the read-only evidence collector."""

    goal: Callable[[Path, str], Mapping[str, Any]] = default_goal_source
    runs: Callable[[Path, Sequence[str]], Sequence[Mapping[str, Any]]] = \
        default_runs_source
    test_record: Callable[[Path], Mapping[str, Any]] = \
        default_test_record_source
    interpreters: Callable[[Path], Sequence[Mapping[str, Any]]] = \
        default_interpreters_source

    def __post_init__(self) -> None:
        for name in ("goal", "runs", "test_record", "interpreters"):
            if not callable(getattr(self, name)):
                raise TypeError(f"evidence source {name} must be callable")


def fact(subject: str, value: Any, *, source: str,
         observed_at: int | str = "unknown", freshness: str = "current",
         evidence: str = "observed") -> C.Fact:
    """Build one evidence fact; values are coerced to canonical JSON."""
    return C.Fact(subject=subject, value=_clean(value), source=source,
                  observed_at=observed_at, freshness=freshness,
                  evidence=evidence)


def _clean(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[depth]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(C.INT64_MIN, min(C.INT64_MAX, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return max(C.INT64_MIN, min(C.INT64_MAX, int(round(value))))
    if isinstance(value, str):
        return _text(value, MAX_REF)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                break
            out[_text(key, 128)] = _clean(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth=depth + 1) for item in value[:128]]
    return _text(value, 128)


# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


def collect_evidence(root: str | Path, goal_id: str, *,
                     run_ids: Iterable[str] = (),
                     test_keys: Iterable[str] = (),
                     sources: Sources | None = None,
                     observed_at: int | str = "unknown",
                     max_criteria: int = MAX_CRITERIA,
                     max_runs: int = MAX_RUNS,
                     max_test_keys: int = MAX_TEST_KEYS,
                     max_interpreters: int = MAX_INTERPRETERS
                     ) -> dict[str, Any]:
    """Collect the bounded, redacted evidence join for one goal."""
    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError(f"repository root does not exist: {root}") from exc
    if not root_path.is_dir():
        raise EvidenceError(f"repository root is not a directory: {root_path}")
    canonical_goal, local_goal = _goal_identity(goal_id)
    if isinstance(observed_at, bool):
        raise EvidenceError("observed_at must be an injected value, not bool")
    if not isinstance(observed_at, (int, str)):
        raise EvidenceError("observed_at must be an integer or a short string")
    if isinstance(observed_at, str) and (not observed_at or len(observed_at) > 64):
        raise EvidenceError("observed_at string must be bounded")
    criteria_limit = _check_bound(max_criteria, "max_criteria", MAX_CRITERIA)
    runs_limit = _check_bound(max_runs, "max_runs", MAX_RUNS)
    keys_limit = _check_bound(max_test_keys, "max_test_keys", MAX_TEST_KEYS)
    interp_limit = _check_bound(max_interpreters, "max_interpreters",
                                MAX_INTERPRETERS)
    requested_runs, runs_omitted = _run_ids(run_ids, runs_limit)
    explicit_keys, keys_omitted = _test_keys(test_keys, keys_limit)
    if sources is not None and not isinstance(sources, Sources):
        raise EvidenceError("sources must be evidence.Sources")
    source_set = sources or Sources()

    goal_source = source_set.goal(root_path, local_goal)
    goal = goal_source.get("goal") if isinstance(goal_source, Mapping) else None
    if not isinstance(goal, Mapping):
        goal = None
    goal_state = str(goal_source.get("state") or "invalid") \
        if isinstance(goal_source, Mapping) else "invalid"
    goal_reason = _line(goal_source.get("reason") or "") \
        if isinstance(goal_source, Mapping) else "the goal source returned no mapping"
    goal_observed_at = _observed(
        goal_source.get("observed_at") if isinstance(goal_source, Mapping)
        else None, observed_at)
    goal_path = (goal_source.get("path") if isinstance(goal_source, Mapping)
                 else None)
    goal_status = str(goal.get("status") or "unknown") if goal else "unknown"
    goal_fact = fact(
        f"evidence.goal.{canonical_goal}",
        {"state": goal_state, "status": goal_status,
         "criteria": len(goal.get("criteria") or []) if goal else 0},
        source="ozagia.core.goals", observed_at=goal_observed_at,
        freshness=("current" if goal_state == "present" else
                   "absent" if goal_state == "absent" else "unknown"),
        evidence="observed" if goal_state != "absent" else "inferred")

    record_source = source_set.test_record(root_path)
    record_state = str(record_source.get("state") or "invalid") \
        if isinstance(record_source, Mapping) else "invalid"
    record = record_source.get("record") if isinstance(record_source, Mapping) \
        else None
    if not isinstance(record, Mapping):
        record = None
    record_observed_at = _observed(
        record_source.get("observed_at") if isinstance(record_source, Mapping)
        else None, observed_at)
    record_fact = fact(
        "evidence.test_record",
        {"state": record_state,
         "runs": len((record or {}).get("runs") or []),
         "tests": len((record or {}).get("tests") or [])},
        source="ozagia.core.working_set.load_test_record",
        observed_at=record_observed_at,
        freshness=("current" if record_state == "present" else
                   "absent" if record_state == "absent" else "unknown"),
        evidence="observed" if record_state != "absent" else "inferred")

    criteria, criteria_total = _criteria_records(
        goal, canonical_goal, record, criteria_limit)
    all_test_keys = sorted(dict.fromkeys(
        list(explicit_keys) + _criterion_test_keys(criteria)))
    test_key_rows = _test_key_records(all_test_keys, record, keys_limit)
    test_keys_omitted = (keys_omitted
                         + max(0, len(all_test_keys) - len(test_key_rows)))
    run_rows = _run_records(source_set, root_path, requested_runs)
    environment_rows, environment_notes, environment_total = (
        _environment_records(source_set, root_path, interp_limit, observed_at))

    criteria_counts = _result_counts(criteria)
    run_counts: dict[str, int] = {}
    for row in run_rows:
        state = str(row.get("state") or "unknown")
        run_counts[state] = run_counts.get(state, 0) + 1
    test_outcomes: dict[str, int] = {}
    for row in test_key_rows:
        outcome = str(row.get("outcome") or "unknown")
        test_outcomes[outcome] = test_outcomes.get(outcome, 0) + 1
    omitted = (max(0, criteria_total - len(criteria)) + runs_omitted
               + test_keys_omitted
               + max(0, environment_total - len(environment_rows)))
    report: dict[str, Any] = {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "report_version": REPORT_VERSION,
        "goal": {
            "id": canonical_goal,
            "local_id": local_goal,
            "state": goal_state,
            "status": goal_status,
            "path": _text(goal_path, MAX_REF) if goal_path else None,
            "observed_at": goal_observed_at,
            "source": "ozagia.core.goals",
            "filtered": False,
            "facts": [goal_fact.to_dict()],
            "reason": goal_reason or None,
        },
        "criteria": criteria,
        "test_record": {
            "state": record_state,
            "path": _text(record_source.get("path"), MAX_REF)
                    if isinstance(record_source, Mapping) else None,
            "observed_at": record_observed_at,
            "digest": _sha(_test_record_view(record or {})),
            "runs": len((record or {}).get("runs") or []),
            "tests": len((record or {}).get("tests") or []),
            "facts": [record_fact.to_dict()],
            "reason": _line(record_source.get("reason") or "")
                      if isinstance(record_source, Mapping) else None,
        },
        "test_keys": test_key_rows,
        "runs": run_rows,
        "environment": environment_rows,
        "coverage": {
            "complete": False,
            "goal": goal_state,
            "criteria": {"total": criteria_total,
                         "returned": len(criteria),
                         "results": criteria_counts},
            "runs": {"total": len(requested_runs),
                     "returned": len(run_rows),
                     "states": run_counts},
            "test_record": record_state,
            "test_keys": {"returned": len(test_key_rows),
                          "outcomes": test_outcomes},
            "interpreters": {
                "returned": len(environment_rows),
                "existing": sum(1 for row in environment_rows
                                if row.get("exists")),
            },
            "unknowns": _coverage_unknowns(environment_notes),
        },
        "not_assessed": sorted({
            "the collector does not re-evaluate whether a criterion's evidence "
            "is still current",
            "a task run or test record does not record the interpreter it used",
            "only runs named by the caller are joined",
            "hashes and redacted references only; prompts, secrets and raw "
            "logs are not collected",
            "durable evidence writes are deferred",
        }),
        "deferred": list(DEFERRED),
        "bounds": {
            "criteria": criteria_limit,
            "runs": runs_limit,
            "test_keys": keys_limit,
            "interpreters": interp_limit,
            "max_report_bytes": MAX_REPORT_BYTES,
            "criteria_omitted": max(0, criteria_total - len(criteria)),
            "runs_omitted": runs_omitted,
            "test_keys_omitted": test_keys_omitted,
        },
        "truncated": (C.truncation(True, omitted=omitted, reason="limit").to_dict()
                      if omitted else C.truncation().to_dict()),
    }
    report = _enforce_bytes(report)
    report["hash"] = C.content_hash(
        {key: value for key, value in report.items() if key != "hash"})
    return report


def _criteria_records(goal: Mapping[str, Any] | None, canonical_goal: str,
                      record: Mapping[str, Any] | None,
                      limit: int) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(goal, Mapping):
        return [], 0
    rows = sorted((item for item in goal.get("criteria") or []
                   if isinstance(item, Mapping)),
                  key=lambda item: tuple(
                      int(part) if part.isdigit() else part
                      for part in re.split(r"(\d+)", str(item.get("id") or ""))))
    total = len(rows)
    out: list[dict[str, Any]] = []
    for criterion in rows[:limit]:
        kind = str(criterion.get("kind") or "unknown")
        args = criterion.get("args") if isinstance(criterion.get("args"),
                                                    Mapping) else {}
        last = criterion.get("last") if isinstance(criterion.get("last"),
                                                    Mapping) else {}
        recorded_status = str(last.get("status") or "unchecked")
        test_join = None
        key = args.get("key") if isinstance(args, Mapping) else None
        if kind in ("test_passes", "test_fails") and isinstance(key, str):
            test_join = _recorded_outcome(record, key)
        freshness = "current" if last else "absent"
        evidence = "observed" if test_join else (
            "declared" if last else "inferred")
        item_fact = fact(
            f"evidence.criterion.{criterion.get('id') or 'unknown'}",
            {"kind": kind, "result": STATUS_MAP.get(
                recorded_status, "unknown"), "test": test_join},
            source="ozagia.core.goals", observed_at=_epoch_ms(last.get("at"))
            or "unknown", freshness=freshness, evidence=evidence)
        out.append({
            "id": str(criterion.get("id") or "unknown"),
            "goal": canonical_goal,
            "kind": kind,
            "args": _criterion_args(kind, args),
            "result": STATUS_MAP.get(recorded_status, "unknown"),
            "recorded_status": recorded_status,
            "freshness": freshness,
            "evidence": evidence,
            "source": "ozagia.core.goals",
            "observed_at": _epoch_ms(last.get("at")) or "unknown",
            "reason": redact_text(last.get("reason") or "", MAX_REASON) or None,
            "test": test_join,
            "interpreter": {
                "state": "not_recorded",
                "reason": "the test record does not record which interpreter "
                          "ran",
            } if kind in ("test_passes", "test_fails") else None,
            "fact": item_fact.to_dict(),
        })
    return out, total


def _criterion_test_keys(criteria: Sequence[Mapping[str, Any]]) -> list[str]:
    keys = []
    for item in criteria:
        test = item.get("test")
        if isinstance(test, Mapping) and isinstance(test.get("key"), str):
            keys.append(test["key"])
        elif isinstance(item.get("args"), Mapping):
            key = item["args"].get("key")
            if isinstance(key, str) and key.startswith("test:"):
                keys.append(key)
    return sorted(dict.fromkeys(keys))


def _recorded_outcome(record: Mapping[str, Any] | None,
                      key: str) -> dict[str, Any] | None:
    if not isinstance(record, Mapping) or not key:
        return {
            "key": key,
            "outcome": "unknown",
            "recorded_at": None,
            "reason": "no test record is available",
            "run": None,
        }
    try:
        from ozagia.core import working_set as W

        got = W.recorded_outcome(dict(record), key, evidence=True)
    except Exception as exc:  # noqa: BLE001 - evidence stays unknown
        return {
            "key": key,
            "outcome": "unknown",
            "recorded_at": None,
            "reason": _line(f"{type(exc).__name__}: {exc}"),
            "run": None,
        }
    run = got.get("run") if isinstance(got.get("run"), Mapping) else None
    run_summary = None
    if run is not None:
        run_summary = {
            "complete": bool(run.get("complete")),
            "filtered": bool(run.get("filtered")),
            "source": _text(run.get("source"), 40),
            "stops": bool(run.get("stops")),
        }
        for name in ("failure_class", "failure_module"):
            if isinstance(run.get(name), str):
                run_summary[name] = _text(run[name], 80)
    return {
        "key": key,
        "outcome": str(got.get("outcome") or "unknown"),
        "recorded_at": _epoch_ms(got.get("ts")),
        "reason": redact_text(got.get("reason") or "", MAX_REASON) or None,
        "run": run_summary,
    }


def _test_key_records(keys: Sequence[str],
                      record: Mapping[str, Any] | None,
                      limit: int) -> list[dict[str, Any]]:
    out = []
    for key in sorted(dict.fromkeys(keys))[:limit]:
        outcome = _recorded_outcome(record, key)
        if outcome is None:
            continue
        item_fact = fact(
            f"evidence.test.{key}", outcome,
            source="ozagia.core.working_set.recorded_outcome",
            observed_at=outcome.get("recorded_at") or "unknown",
            freshness="current" if outcome.get("run") else "unknown",
            evidence="observed")
        out.append({
            "key": key,
            "outcome": outcome.get("outcome"),
            "recorded_at": outcome.get("recorded_at"),
            "reason": outcome.get("reason"),
            "run": outcome.get("run"),
            "source": "ozagia.core.working_set.recorded_outcome",
            "observed_at": outcome.get("recorded_at") or "unknown",
            "interpreter": {
                "state": "not_recorded",
                "reason": "the test record does not record which interpreter "
                          "ran",
            },
            "fact": item_fact.to_dict(),
        })
    return out


def _run_records(sources: Sources, root: Path,
                 run_ids: Sequence[str]) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    try:
        raw_rows = list(sources.runs(root, run_ids))
    except Exception as exc:  # noqa: BLE001 - every requested run is reported
        reason = _line(f"the run source failed: "
                       f"{type(exc).__name__}: {exc}")
        return [_run_record(run_id, {"state": "invalid", "reason": reason})
                for run_id in run_ids]
    by_id = {}
    for row in raw_rows:
        if isinstance(row, Mapping) and isinstance(row.get("run_id"), str):
            by_id[row["run_id"]] = row
    out = []
    for run_id in run_ids:
        row = by_id.get(run_id) or {
            "run_id": run_id, "state": "absent",
            "reason": "the run source returned no record", "observed_at":
            "unknown",
        }
        out.append(_run_record(run_id, row))
    return out


def _run_record(run_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "invalid")
    if state == "complete":
        state = "present"
    observed_at = _observed(row.get("observed_at"), "unknown")
    reason = redact_text(row.get("reason") or "", MAX_REASON) or None
    run = row.get("run") if isinstance(row.get("run"), Mapping) else None
    manifest = (run.get("manifest") if isinstance(run, Mapping)
                and isinstance(run.get("manifest"), Mapping) else {})
    events = (run.get("events") if isinstance(run, Mapping)
              and isinstance(run.get("events"), list) else [])
    artifacts = (run.get("artifacts") if isinstance(run, Mapping)
                 and isinstance(run.get("artifacts"), list) else [])
    labels: list[dict[str, str]] = []
    event_counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        kind = redact_text(event.get("type") or "unknown", 60)
        event_counts[kind] = event_counts.get(kind, 0) + 1
        if kind == "evidence" and len(labels) < MAX_LABELS:
            label = redact_text(event.get("label") or "", 200)
            label_kind = redact_text(event.get("kind") or "", 60)
            if label:
                labels.append({"kind": label_kind, "label": label})
    artifact_rows = []
    for artifact in artifacts[:MAX_LABELS]:
        if not isinstance(artifact, Mapping):
            continue
        entry = {"path": redact_text(artifact.get("path") or "", 300)}
        digest = _safe_sha(artifact.get("sha256"))
        if digest:
            entry["sha256"] = digest
        artifact_rows.append(entry)
    output = run.get("output") if isinstance(run, Mapping) else None
    output_digest = _sha(output) if output is not None else None
    output_manifest = (manifest.get("output")
                       if isinstance(manifest.get("output"), Mapping) else {})
    manifest_digest = _safe_sha(output_manifest.get("digest"))
    body = {
        "run_id": run_id,
        "state": state,
        "status": _text(manifest.get("status"), 40) or None,
        "stop": _text(manifest.get("stop"), 40) or None,
        "started_at": _text(manifest.get("started_at"), 64) or None,
        "finished_at": _text(manifest.get("finished_at"), 64) or None,
        "duration_ms": _duration_ms(manifest.get("duration_s")),
        "steps": len(manifest.get("steps") or []),
        "evidence_count": (manifest.get("evidence_count")
                           if isinstance(manifest.get("evidence_count"), int)
                           else None),
        "artifacts": artifact_rows,
        "labels": labels,
        "output_digest": output_digest or manifest_digest,
        "events": {"total": len(events),
                   "types": {key: event_counts[key]
                             for key in sorted(event_counts)}},
    }
    digest = C.content_hash(_clean(body))
    freshness = ("current" if state == "present" else
                 "absent" if state == "absent" else "unknown")
    item_fact = fact(
        f"evidence.run.{run_id}", {**body, "reason": reason},
        source="ozagia.core.tasks.show_run", observed_at=observed_at,
        freshness=freshness,
        evidence="observed" if state != "absent" else "inferred")
    return {
        "run_id": run_id,
        "state": state,
        "path": _text(row.get("path"), MAX_REF) or None,
        "observed_at": observed_at,
        "freshness": freshness,
        "evidence": "observed" if state != "absent" else "inferred",
        "source": "ozagia.core.tasks.show_run",
        "reason": reason,
        "digest": digest,
        "status": body["status"],
        "stop": body["stop"],
        "started_at": body["started_at"],
        "finished_at": body["finished_at"],
        "duration_ms": body["duration_ms"],
        "steps": body["steps"],
        "evidence_count": body["evidence_count"],
        "artifacts": artifact_rows,
        "labels": labels,
        "output_digest": body["output_digest"],
        "events": body["events"],
        "fact": item_fact.to_dict(),
    }


def _environment_records(sources: Sources, root: Path, limit: int,
                         observed_at: int | str
                         ) -> tuple[list[dict[str, Any]], list[str], int]:
    notes: list[str] = []
    try:
        raw = list(sources.interpreters(root))
    except Exception as exc:  # noqa: BLE001 - an unavailable source is visible
        return [], [f"interpreter discovery failed: {type(exc).__name__}"], 0
    try:
        from ozagia.core import environments as ENV

        trusted = ENV.trust_project_python(root)
    except Exception:  # noqa: BLE001 - trust defaults to false
        trusted = False
    rows: list[dict[str, Any]] = []
    valid = 0
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if item.get("state") == "unavailable":
            notes.append(_line(item.get("reason")
                               or "interpreter discovery was unavailable"))
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        valid += 1
        project = bool(item.get("project"))
        exists = False
        try:
            exists = Path(path).is_file()
        except OSError:
            exists = False
        record = {
            "path": redact_text(path, MAX_REF),
            "source": _text(item.get("source") or "unknown", 120),
            "evidence": redact_text(item.get("evidence") or "", 200),
            "project": project,
            "trusted": trusted if project else True,
            "exists": exists,
        }
        record_fact = fact(
            f"evidence.environment.{redact_text(path, 120)}",
            {"exists": exists, "project": project,
             "trusted": record["trusted"], "source": record["source"]},
            source="ozagia.core.environments.discover",
            observed_at=_mtime(Path(path)) if exists else observed_at,
            freshness="current" if exists else "absent",
            evidence="observed")
        record["observed_at"] = record_fact.observed_at
        record["freshness"] = record_fact.freshness
        record["fact"] = record_fact.to_dict()
        if len(rows) < limit:
            rows.append(record)
    return rows, notes, valid


def _result_counts(criteria: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in criteria:
        result = str(item.get("result") or "unknown")
        counts[result] = counts.get(result, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _coverage_unknowns(notes: Sequence[str]) -> list[str]:
    unknown = [
        "the collector does not prove a criterion's evidence is still current",
        "the interpreter used by a recorded test run is not recorded",
        "run and test evidence is metadata and hashes, not a result",
        "durable evidence writes are deferred",
    ]
    unknown.extend(str(item) for item in notes if item)
    return sorted(set(unknown))


def _enforce_bytes(report: dict[str, Any]) -> dict[str, Any]:
    def too_big() -> bool:
        return len(C.canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES

    if not too_big():
        report["bounds"]["report_bytes"] = len(
            C.canonical_json(report).encode("utf-8"))
        return report
    dropped = 0
    for section in ("environment", "runs", "test_keys", "criteria"):
        while report[section] and too_big():
            report[section].pop()
            dropped += 1
    report["bounds"]["bytes_dropped"] = dropped
    report["bounds"]["report_bytes"] = len(
        C.canonical_json(report).encode("utf-8"))
    if dropped:
        previous = report["truncated"]
        omitted = int(previous.get("omitted") or 0) + dropped
        report["truncated"] = C.truncation(
            True, omitted=omitted, reason="bytes").to_dict()
    return report


def evidence_json(report: Mapping[str, Any]) -> str:
    """Canonical JSON for an evidence report."""
    return C.canonical_json(dict(report))


def evidence_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical report (the ``hash`` field excluded)."""
    return C.content_hash({key: value for key, value in report.items()
                           if key != "hash"})


def attach_store(report: Mapping[str, Any], root: str | Path, *,
                 limit: int = 10, include_partial: bool = False
                 ) -> dict[str, Any]:
    """Return a copy of an evidence report with the durable store view.

    This is an explicit, additive join: ``collect_evidence`` still never
    writes or reads the store by default.  The base report's own fields are
    left intact (including its ``deferred`` list, which describes the
    collector); only ``durable`` is added and ``hash`` is recomputed because
    it covers the whole document.  Partial records are never listed as
    current.
    """
    from ozagia.core import evidence_store as ES

    if not isinstance(report, Mapping):
        raise EvidenceError("report must be an evidence report mapping")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise EvidenceError("limit must be an integer")
    if limit < 1 or limit > ES.MAX_RECORDS:
        raise EvidenceError(f"limit must be between 1 and {ES.MAX_RECORDS}")
    goal_row = report.get("goal") if isinstance(report.get("goal"),
                                                Mapping) else {}
    goal_id = goal_row.get("id") if isinstance(goal_row, Mapping) else None
    try:
        view = ES.read_store(root, limit=limit, goal=goal_id,
                             include_partial=include_partial)
    except ES.EvidenceStoreError as exc:
        raise EvidenceError(
            f"the durable evidence store refused the join: {exc}",
            code=getattr(exc, "code", "invalid_request")) from exc
    result = dict(report)
    result.pop("hash", None)
    result["durable"] = {
        "schema": view["schema"],
        "state": view["state"],
        "path": view["path"],
        "scope": {"goal": goal_id},
        "observed_at": view["observed_at"],
        "records": view["records"],
        "partial": view["partial"],
        "counts": view["counts"],
        "truncated": view["truncated"],
        "reasons": view["reasons"],
        "unknowns": view["unknowns"],
        "writes": "separate store; this collector remains read-only",
    }
    result["hash"] = evidence_hash(result)
    return result


__all__ = [
    "DEFERRED", "EVIDENCE_KINDS", "EvidenceError", "MAX_CRITERIA",
    "MAX_INTERPRETERS", "MAX_LABELS", "MAX_REPORT_BYTES", "MAX_RUNS",
    "MAX_TEST_KEYS", "REPORT_VERSION", "SCHEMA_NAMESPACE", "SCHEMA_VERSION",
    "Sources", "attach_store", "collect_evidence", "default_goal_source",
    "default_interpreters_source", "default_runs_source",
    "default_test_record_source", "evidence_hash", "evidence_json", "fact",
    "redact_text",
]
