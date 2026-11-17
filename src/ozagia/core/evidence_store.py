"""Durable, bounded evidence references for goals, runs, tests and interpreters.

This module is the additive write side of the WP2 evidence spine.  The
read-only collector in :mod:`ozagia.core.evidence` is unchanged; this store
records *references and redacted metadata* that join records already owned by
the goal, task, working-set and environment modules.  It never mutates those
stores, never runs a test or probe, never calls a model and never writes
outside ``<root>/.ozagia/evidence/``.

Record kinds
------------

``observation``
    A bounded snapshot of the read-only evidence join: goal, criteria, named
    task runs, test-record outcomes and interpreter candidates.  Its
    ``evidence`` is ``observed`` unless the snapshot also satisfies the
    recorded test/run/goal join checks, in which case it is ``verified``.
``assessment``
    A host-produced task-preflight report (or a report with the same
    versioned shape).  A green report is accepted only when every required
    dimension is ``true``; the store never turns an unknown or failed
    preflight into a green record.
``declaration``
    A caller or model claim.  It is stored as ``evidence="declared"`` with
    ``freshness="unknown"`` and is never current evidence.

Evidence words
--------------

``declared``
    Only a claim ("the tests pass", "this plan will fix it").  It is not a
    result and never counts as a pass.
``observed``
    Read from an existing owner record (goal store, task run manifest,
    working-set test record, interpreter discovery).  It says what was
    recorded, not that the work is correct.
``verified``
    The store re-read the owners and the recorded references agree: a
    recorded test outcome matches the criterion direction, the recorded run
    is complete and unfiltered, the criterion is current and at least one
    existing trusted interpreter candidate was found.  It still does **not**
    mean the test was re-executed in this process.

Persistence, idempotency and recovery
-------------------------------------

Records are one canonical-JSON line each in
``.ozagia/evidence/records.jsonl``.  Appends and compaction take the same
``fcntl.flock`` lock pattern as the goal store; compaction replaces the file
atomically (same-directory temp file, ``fsync``, ``os.replace``).  A record's
default idempotency key is the content hash of the record without its local
``record_id``, ``created_at`` and integrity fields, so retrying the same event
returns the stored record instead of appending a duplicate.  Reusing an
explicit key with different content is a ``conflict``.

A crash can leave a torn final line.  Readers report the store as
``partial``, never serve the torn line, and still serve earlier complete
records; a newer partial record also stops an older record with the same
subject from being ``current``.  The next append recovers the torn tail under
the lock before writing.  Malformed interior lines, wrong record versions and
oversized stores fail closed (``invalid`` or ``unsupported``) rather than being
silently skipped.

Bounds: 16 KiB per record, at most 500 records and 8 MiB in the journal.
Appending compacts deterministically: records older than the retention age
are evicted first, then the oldest records above the count limit.  The
just-appended record is never evicted by its own write.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.core import contracts as C
from ozagia.core import evidence as E

STORE_NAMESPACE = "ozagia.evidence.store"
STORE_VERSION = 1
RECORD_VERSION = 1

EVIDENCE_DIR = "evidence"
JOURNAL_NAME = "records.jsonl"
LOCK_NAME = ".lock"

RECORD_KINDS = ("assessment", "declaration", "observation")
STORE_STATES = ("absent", "current", "partial", "invalid", "unsupported")
RECORD_EVIDENCE = ("declared", "observed", "verified")
RUN_STATES = ("absent", "incomplete", "invalid", "present", "unknown")
TEST_OUTCOMES = ("failed", "not_assessed", "passed", "unknown")
ASSESSMENTS = ("failed", "green", "unknown")
ASSESSMENT_DIMENSIONS = (
    "goal", "graph", "capabilities", "environment", "resources", "tests",
)
VERIFICATIONS = ("unverified", "verified")

MAX_RECORD_BYTES = 16 * 1024
MAX_RECORDS = 500
MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
MAX_STORE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 512
MAX_REASON = 300
MAX_REF = 512
MAX_CRITERIA = E.MAX_CRITERIA
MAX_RUNS = E.MAX_RUNS
MAX_TESTS = E.MAX_TEST_KEYS
MAX_ENVIRONMENTS = E.MAX_INTERPRETERS
MAX_REASONS = 20
MAX_CHECKS = 16
LOCK_TIMEOUT_S = 30.0

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
TEST_KEY_RE = re.compile(r"test:[^\s]{1,299}\Z")
RECORD_ID_RE = re.compile(r"ev-[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SHA_RE = re.compile(r"[0-9a-f]{12,64}\Z")

_TOP_FIELDS = frozenset({
    "schema", "record_version", "record_id", "idempotency_key", "kind",
    "source", "observed_at", "created_at", "evidence", "freshness",
    "complete", "reason", "subject", "goal", "criteria", "runs", "tests",
    "environments", "assessment", "declaration", "verification", "refs",
    "record_hash",
})
_INPUT_FIELDS = frozenset({
    "schema", "record_version", "kind", "source", "observed_at", "evidence",
    "freshness", "complete", "reason", "goal", "criteria", "runs", "tests",
    "environments", "assessment", "declaration", "verification", "refs",
    "idempotency_key",
})
_REF_KEYS = frozenset({
    "declared_hash", "goal_hash", "output_digest", "preflight_hash",
    "report_hash", "run_digest", "snapshot", "source_hash",
    "test_record_digest",
})
_GOAL_KEYS = frozenset({"id", "criterion", "result", "state", "status"})
_CRITERION_KEYS = frozenset({
    "id", "kind", "result", "freshness", "evidence", "test_key",
    "recorded_status",
})
_RUN_KEYS = frozenset({
    "run_id", "state", "status", "digest", "output_digest",
})
_TEST_KEYS = frozenset({"key", "outcome", "recorded_at", "run"})
_TEST_RUN_KEYS = frozenset({"complete", "filtered", "source"})
_ENVIRONMENT_KEYS = frozenset({
    "path", "source", "project", "trusted", "exists", "freshness",
})
_ASSESSMENT_KEYS = frozenset({
    "status", "green", "dimensions", "criteria", "reasons", "environment",
})
_ASSESSMENT_DIMENSION_KEYS = frozenset({"readiness", "reason"})
_ASSESSMENT_CRITERIA_KEYS = frozenset({"total", "counts"})
_ASSESSMENT_ENVIRONMENT_KEYS = frozenset({
    "selected", "candidates", "readiness", "reason",
})
_DECLARATION_KEYS = frozenset({"statement"})
_VERIFICATION_KEYS = frozenset({"state", "method", "checks", "reasons"})
_CHECK_KEYS = frozenset({"name", "state"})
_CHECK_STATES = frozenset({"failed", "passed", "unknown"})

_THREAD_LOCK = threading.Lock()


class EvidenceStoreError(ValueError):
    """A malformed record, refusal or unavailable store with a stable code."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        self.code = code if code in C.ERROR_CODES else "invalid_request"
        super().__init__(_line(message))


# ---------------------------------------------------------------------------
# Bounded helpers
# ---------------------------------------------------------------------------


def _line(value: Any, limit: int = MAX_REASON) -> str:
    return " ".join(str(value).split())[:limit]


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    return text[:limit]


def _redacted(value: Any, limit: int = MAX_REASON) -> str:
    return E.redact_text(_text(value, limit * 2), limit)


def _sha(value: Any) -> str | None:
    if isinstance(value, str) and SHA_RE.fullmatch(value):
        return value
    return None


def _observed_at(value: Any) -> int | str:
    if isinstance(value, bool):
        raise EvidenceStoreError("observed_at must not be a bool")
    if isinstance(value, int):
        if value < 0 or value > C.INT64_MAX:
            raise EvidenceStoreError("observed_at is out of range")
        return value
    if isinstance(value, str):
        text = _text(value, 64)
        if not text or len(text) > 64 or text != value:
            raise EvidenceStoreError("observed_at string must be bounded")
        return text
    raise EvidenceStoreError("observed_at must be an integer or a string")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _check_ms(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceStoreError(f"{name} must be an integer")
    if value < 0 or value > C.INT64_MAX:
        raise EvidenceStoreError(f"{name} is out of range")
    return value


def _check_limit(value: Any, name: str, hard: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceStoreError(f"{name} must be an integer")
    if value < 1 or value > hard:
        raise EvidenceStoreError(f"{name} must be between 1 and {hard}")
    return value


def _check_age(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceStoreError("max_age_ms must be an integer")
    if value < 0 or value > MAX_AGE_MS:
        raise EvidenceStoreError(
            f"max_age_ms must be between 0 and {MAX_AGE_MS}")
    return value


def _keys(value: Mapping[str, Any], allowed: frozenset[str],
          where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvidenceStoreError(
            f"{where} has unknown fields: {unknown}", code="unknown_field")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceStoreError(f"{where} must be a mapping")
    return value


def _list(value: Any, where: str, limit: int) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise EvidenceStoreError(f"{where} must be a list")
    items = list(value)
    if len(items) > limit:
        raise EvidenceStoreError(
            f"{where} has {len(items)} items; the bound is {limit}",
            code="budget_exceeded")
    return items


def _bool(value: Any, where: str) -> bool:
    if value is not True and value is not False:
        raise EvidenceStoreError(f"{where} must be a bool")
    return value


def _nonneg_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceStoreError(f"{where} must be an integer")
    if value < 0 or value > C.INT64_MAX:
        raise EvidenceStoreError(f"{where} is out of range")
    return value


def _goal_identity(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceStoreError("a goal id is required")
    value = value.strip()
    try:
        if value.startswith("goal:"):
            return C.parse_id(value).canonical
        return C.format_id("goal", value)
    except C.ContractError as exc:
        raise EvidenceStoreError(f"invalid goal id {value!r}: {exc}") from exc


def _goal_filter(value: Any) -> str:
    return _goal_identity(value)


def _run_filter(value: Any) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise EvidenceStoreError(
            "run ids must match [A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
    return value


def _source(value: Any) -> str:
    if not isinstance(value, str):
        raise EvidenceStoreError("a record source must be a string")
    text = _redacted(value, 120)
    if not text:
        raise EvidenceStoreError("a record source is required")
    return text


# ---------------------------------------------------------------------------
# Record sections
# ---------------------------------------------------------------------------


def _goal_ref(value: Any) -> dict[str, Any]:
    row = _mapping(value, "goal")
    _keys(row, _GOAL_KEYS, "goal")
    out: dict[str, Any] = {"id": _goal_identity(row.get("id"))}
    for name in ("criterion", "status"):
        if row.get(name) is not None:
            out[name] = _redacted(row.get(name), 80)
    if row.get("state") is not None:
        state = row["state"]
        if state not in ("absent", "invalid", "present", "unknown"):
            raise EvidenceStoreError("goal.state has an unknown value")
        out["state"] = state
    if row.get("result") is not None:
        result = row["result"]
        if result not in C.RESULTS:
            raise EvidenceStoreError("goal.result has an unknown value")
        out["result"] = result
    return out


def _criteria_refs(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "criteria", MAX_CRITERIA)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"criteria[{index}]")
        _keys(row, _CRITERION_KEYS, f"criteria[{index}]")
        criterion = {
            "id": _redacted(row.get("id"), 64),
            "kind": _redacted(row.get("kind"), 60),
            "result": row.get("result"),
            "freshness": row.get("freshness"),
            "evidence": row.get("evidence"),
            "test_key": None,
            "recorded_status": _redacted(row.get("recorded_status"), 40),
        }
        if not criterion["id"]:
            raise EvidenceStoreError(f"criteria[{index}] needs an id")
        if criterion["result"] not in C.RESULTS:
            raise EvidenceStoreError(
                f"criteria[{index}].result has an unknown value")
        if criterion["freshness"] not in C.FRESHNESS:
            raise EvidenceStoreError(
                f"criteria[{index}].freshness has an unknown value")
        if criterion["evidence"] not in C.EVIDENCE:
            raise EvidenceStoreError(
                f"criteria[{index}].evidence has an unknown value")
        if row.get("test_key") is not None:
            key = row["test_key"]
            if not isinstance(key, str) or not TEST_KEY_RE.fullmatch(key):
                raise EvidenceStoreError(
                    f"criteria[{index}].test_key is not a test key")
            criterion["test_key"] = key
        out.append(criterion)
    return out


def _run_refs(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "runs", MAX_RUNS)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"runs[{index}]")
        _keys(row, _RUN_KEYS, f"runs[{index}]")
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise EvidenceStoreError(f"runs[{index}].run_id is invalid")
        state = row.get("state")
        if state not in RUN_STATES:
            raise EvidenceStoreError(f"runs[{index}].state is invalid")
        out.append({
            "run_id": run_id,
            "state": state,
            "status": _redacted(row.get("status"), 40) or None,
            "digest": _sha(row.get("digest")),
            "output_digest": _sha(row.get("output_digest")),
        })
    return out


def _test_run_ref(value: Any, where: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _mapping(value, where)
    _keys(row, _TEST_RUN_KEYS, where)
    return {
        "complete": _bool(row.get("complete", False), f"{where}.complete"),
        "filtered": _bool(row.get("filtered", False), f"{where}.filtered"),
        "source": _redacted(row.get("source"), 40),
    }


def _test_refs(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "tests", MAX_TESTS)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"tests[{index}]")
        _keys(row, _TEST_KEYS, f"tests[{index}]")
        key = row.get("key")
        if not isinstance(key, str) or not TEST_KEY_RE.fullmatch(key):
            raise EvidenceStoreError(f"tests[{index}].key is not a test key")
        outcome = row.get("outcome")
        if outcome not in TEST_OUTCOMES:
            raise EvidenceStoreError(f"tests[{index}].outcome is invalid")
        recorded_at = row.get("recorded_at")
        if recorded_at is not None:
            recorded_at = _nonneg_int(recorded_at, f"tests[{index}].recorded_at")
        out.append({
            "key": key,
            "outcome": outcome,
            "recorded_at": recorded_at,
            "run": _test_run_ref(row.get("run"), f"tests[{index}].run"),
        })
    return out


def _environment_refs(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "environments", MAX_ENVIRONMENTS)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"environments[{index}]")
        _keys(row, _ENVIRONMENT_KEYS, f"environments[{index}]")
        path = _redacted(row.get("path"), MAX_REF)
        if not path:
            raise EvidenceStoreError(f"environments[{index}].path is required")
        freshness = row.get("freshness", "unknown")
        if freshness not in C.FRESHNESS:
            raise EvidenceStoreError(
                f"environments[{index}].freshness is invalid")
        out.append({
            "path": path,
            "source": _redacted(row.get("source"), 120),
            "project": _bool(row.get("project", False),
                             f"environments[{index}].project"),
            "trusted": _bool(row.get("trusted", False),
                             f"environments[{index}].trusted"),
            "exists": _bool(row.get("exists", False),
                            f"environments[{index}].exists"),
            "freshness": freshness,
        })
    return out


def _assessment_environment(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _mapping(value, "assessment.environment")
    _keys(row, _ASSESSMENT_ENVIRONMENT_KEYS, "assessment.environment")
    selected = row.get("selected")
    readiness = row.get("readiness")
    if readiness is not True and readiness is not False \
            and readiness != "unknown":
        raise EvidenceStoreError(
            "assessment.environment.readiness must be true, false or unknown")
    candidates = _nonneg_int(row.get("candidates", 0),
                             "assessment.environment.candidates")
    return {
        "selected": _redacted(selected, MAX_REF) if selected else None,
        "candidates": candidates,
        "readiness": readiness,
        "reason": _redacted(row.get("reason"), MAX_REASON),
    }


def _assessment_ref(value: Any) -> dict[str, Any]:
    row = _mapping(value, "assessment")
    _keys(row, _ASSESSMENT_KEYS, "assessment")
    status = row.get("status")
    if status not in ASSESSMENTS:
        raise EvidenceStoreError("assessment.status is invalid")
    green = _bool(row.get("green", False), "assessment.green")
    if green != (status == "green"):
        raise EvidenceStoreError(
            "assessment.green must be true exactly when the status is green")
    dimensions = _mapping(row.get("dimensions"), "assessment.dimensions")
    _keys(dimensions, frozenset(ASSESSMENT_DIMENSIONS),
          "assessment.dimensions")
    missing = [name for name in ASSESSMENT_DIMENSIONS if name not in dimensions]
    if missing:
        raise EvidenceStoreError(
            f"assessment.dimensions is missing {missing}")
    dim_out: dict[str, dict[str, Any]] = {}
    for name in ASSESSMENT_DIMENSIONS:
        item = _mapping(dimensions[name], f"assessment.dimensions.{name}")
        _keys(item, _ASSESSMENT_DIMENSION_KEYS,
              f"assessment.dimensions.{name}")
        readiness = item.get("readiness")
        if readiness is not True and readiness is not False \
                and readiness != "unknown":
            raise EvidenceStoreError(
                f"assessment.dimensions.{name}.readiness is invalid")
        dim_out[name] = {
            "readiness": readiness,
            "reason": _redacted(item.get("reason"), MAX_REASON),
        }
    reasons = []
    for index, item in enumerate(_list(row.get("reasons", []),
                                       "assessment.reasons", MAX_REASONS)):
        reasons.append(_redacted(item, MAX_REASON))
    criteria = row.get("criteria")
    criteria_out = None
    if criteria is not None:
        item = _mapping(criteria, "assessment.criteria")
        _keys(item, _ASSESSMENT_CRITERIA_KEYS, "assessment.criteria")
        counts = _mapping(item.get("counts", {}), "assessment.criteria.counts")
        count_out: dict[str, int] = {}
        for key, count in list(counts.items())[:64]:
            if not isinstance(key, str) or key not in C.RESULTS:
                raise EvidenceStoreError(
                    "assessment.criteria.counts has an unknown result")
            count_out[key] = _nonneg_int(
                count, f"assessment.criteria.counts.{key}")
        criteria_out = {
            "total": _nonneg_int(item.get("total", 0),
                                 "assessment.criteria.total"),
            "counts": count_out,
        }
    if green:
        not_true = [name for name in ASSESSMENT_DIMENSIONS
                    if dim_out[name]["readiness"] is not True]
        if not_true or reasons:
            raise EvidenceStoreError(
                "a green assessment requires every dimension to be true and "
                "no non-true reasons", code="invalid_state")
    return {
        "status": status,
        "green": green,
        "dimensions": dim_out,
        "criteria": criteria_out,
        "reasons": reasons,
        "environment": _assessment_environment(row.get("environment")),
    }


def _declaration_ref(value: Any) -> dict[str, Any]:
    row = _mapping(value, "declaration")
    _keys(row, _DECLARATION_KEYS, "declaration")
    if not isinstance(row.get("statement"), str):
        raise EvidenceStoreError("a declaration statement must be a string")
    statement = _redacted(row.get("statement"), MAX_REASON)
    if not statement:
        raise EvidenceStoreError("a declaration needs a bounded statement")
    return {"statement": statement}


def _verification_ref(value: Any) -> dict[str, Any]:
    row = _mapping(value, "verification")
    _keys(row, _VERIFICATION_KEYS, "verification")
    state = row.get("state")
    if state not in VERIFICATIONS:
        raise EvidenceStoreError("verification.state is invalid")
    checks = []
    for index, item in enumerate(_list(row.get("checks", []),
                                       "verification.checks", MAX_CHECKS)):
        check = _mapping(item, f"verification.checks[{index}]")
        _keys(check, _CHECK_KEYS, f"verification.checks[{index}]")
        name = _redacted(check.get("name"), 60)
        if not name:
            raise EvidenceStoreError("a verification check needs a name")
        if check.get("state") not in _CHECK_STATES:
            raise EvidenceStoreError(
                f"verification.checks[{index}].state is invalid")
        checks.append({"name": name, "state": check["state"]})
    reasons = [_redacted(item, MAX_REASON) for item in
               _list(row.get("reasons", []), "verification.reasons",
                     MAX_REASONS)]
    return {
        "state": state,
        "method": _redacted(row.get("method"), 60) or None,
        "checks": checks,
        "reasons": reasons,
    }


def _refs(value: Any) -> dict[str, Any]:
    row = _mapping(value, "refs")
    _keys(row, _REF_KEYS, "refs")
    out: dict[str, Any] = {}
    for key in sorted(row):
        item = row[key]
        if item is None:
            continue
        if key == "snapshot":
            text = _redacted(item, MAX_REF)
            if not text:
                continue
            out[key] = text
            continue
        digest = _sha(item)
        if digest is None:
            raise EvidenceStoreError(f"refs.{key} must be a SHA-256 prefix")
        out[key] = digest
    return out


def _declared_verification() -> dict[str, Any]:
    return {
        "state": "unverified",
        "method": None,
        "checks": [],
        "reasons": ["a declaration is a claim, not evidence"],
    }


def _subject(kind: str, goal: Mapping[str, Any] | None,
             runs: Sequence[Mapping[str, Any]],
             tests: Sequence[Mapping[str, Any]],
             declaration: Mapping[str, Any] | None) -> str:
    if goal:
        base = f"goal:{goal['id']}"
    elif runs:
        base = f"run:{runs[0]['run_id']}"
    elif tests:
        base = f"test:{tests[0]['key']}"
    else:
        base = "global"
    if kind == "declaration" and declaration:
        return f"declaration:{base}"
    return f"{kind}:{base}"


# ---------------------------------------------------------------------------
# Record construction and validation
# ---------------------------------------------------------------------------


def _normalize_record(raw: Mapping[str, Any], *,
                      now_ms: int | None = None,
                      idempotency_key: str | None = None
                      ) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise EvidenceStoreError("a record must be a mapping")
    _keys(raw, _INPUT_FIELDS, "record")
    if raw.get("schema") is not None and raw.get("schema") != STORE_NAMESPACE:
        raise EvidenceStoreError(
            f"record schema must be {STORE_NAMESPACE!r}",
            code="unsupported_version")
    if raw.get("record_version") is not None:
        version = raw.get("record_version")
        if version != RECORD_VERSION:
            raise EvidenceStoreError(
                f"record version {version!r} is not supported",
                code="unsupported_version")
    kind = raw.get("kind")
    if kind not in RECORD_KINDS:
        raise EvidenceStoreError(
            f"record kind must be one of {list(RECORD_KINDS)}")
    source = _source(raw.get("source"))
    observed_at = _observed_at(raw.get("observed_at", "unknown"))
    complete = _bool(raw.get("complete", True), "complete")
    goal = _goal_ref(raw["goal"]) if raw.get("goal") is not None else None
    runs = _run_refs(raw["runs"]) if raw.get("runs") is not None else []
    tests = _test_refs(raw["tests"]) if raw.get("tests") is not None else []
    criteria = (_criteria_refs(raw["criteria"])
                if raw.get("criteria") is not None else [])
    environments = (_environment_refs(raw["environments"])
                    if raw.get("environments") is not None else [])
    assessment = (_assessment_ref(raw["assessment"])
                  if raw.get("assessment") is not None else None)
    declaration = (_declaration_ref(raw["declaration"])
                   if raw.get("declaration") is not None else None)
    refs = _refs(raw["refs"]) if raw.get("refs") is not None else {}
    reason = _redacted(raw.get("reason"), MAX_REASON) or None

    if kind == "declaration":
        if declaration is None:
            raise EvidenceStoreError("a declaration record needs a declaration")
        if assessment is not None or criteria or environments:
            raise EvidenceStoreError(
                "a declaration cannot carry observed sections")
        if any(run.get("state") != "unknown" or run.get("status")
               or run.get("digest") or run.get("output_digest")
               for run in runs):
            raise EvidenceStoreError(
                "a declaration cannot carry observed run results")
        if any(test.get("outcome") != "unknown"
               or test.get("recorded_at") is not None
               or test.get("run") is not None for test in tests):
            raise EvidenceStoreError(
                "a declaration cannot carry observed test results")
        evidence = "declared"
        freshness = "unknown"
        verification = _declared_verification()
    elif kind == "assessment":
        if assessment is None:
            raise EvidenceStoreError("an assessment record needs an assessment")
        if declaration is not None:
            raise EvidenceStoreError("an assessment is not a declaration")
        evidence = "verified" if assessment["green"] else "observed"
        freshness = ("current" if assessment["status"] in ("green", "failed")
                     else "unknown")
        if raw.get("verification") is None:
            verification = {
                "state": ("verified" if assessment["green"]
                          else "unverified"),
                "method": "task_preflight_assessment",
                "checks": [{
                    "name": "required_dimensions",
                    "state": ("passed" if assessment["green"] else "failed"),
                }],
                "reasons": ([] if assessment["green"]
                            else ["the assessment is not green"]),
            }
        else:
            verification = _verification_ref(raw["verification"])
    else:
        if declaration is not None or assessment is not None:
            raise EvidenceStoreError(
                "an observation cannot carry declaration/assessment sections")
        evidence = raw.get("evidence")
        if evidence not in ("observed", "verified"):
            raise EvidenceStoreError(
                "an observation's evidence must be observed or verified")
        freshness = raw.get("freshness")
        if freshness not in C.FRESHNESS:
            raise EvidenceStoreError(
                f"freshness must be one of {list(C.FRESHNESS)}")
        if raw.get("verification") is None:
            raise EvidenceStoreError(
                "an observation record needs a verification section")
        verification = _verification_ref(raw["verification"])
        if evidence == "verified" and verification["state"] != "verified":
            raise EvidenceStoreError(
                "verified evidence needs a verified verification section",
                code="invalid_state")

    if not complete and freshness == "current":
        freshness = "unknown"
    if not complete and evidence == "verified":
        evidence = "observed"
    if evidence == "verified" and verification["state"] != "verified":
        raise EvidenceStoreError(
            "verified evidence needs a verified verification section",
            code="invalid_state")
    if evidence == "observed" and verification["state"] == "verified":
        raise EvidenceStoreError(
            "an observed record cannot carry a verified verification section",
            code="invalid_state")

    body: dict[str, Any] = {
        "record_version": RECORD_VERSION,
        "schema": STORE_NAMESPACE,
        "kind": kind,
        "source": source,
        "observed_at": observed_at,
        "evidence": evidence,
        "freshness": freshness,
        "complete": complete,
        "reason": reason,
        "subject": _subject(kind, goal, runs, tests, declaration),
        "goal": goal,
        "criteria": criteria,
        "runs": runs,
        "tests": tests,
        "environments": environments,
        "assessment": assessment,
        "declaration": declaration,
        "verification": verification,
        "refs": refs,
    }
    size = len(C.canonical_json(body).encode("utf-8"))
    if size > MAX_RECORD_BYTES:
        raise EvidenceStoreError(
            f"record is {size} bytes; the bound is {MAX_RECORD_BYTES}",
            code="budget_exceeded")

    if idempotency_key is None:
        key = C.content_hash(body)
    else:
        if (not isinstance(idempotency_key, str)
                or not IDEMPOTENCY_RE.fullmatch(idempotency_key)):
            raise EvidenceStoreError(
                "idempotency_key must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
        key = idempotency_key
    record_id = "ev-" + C.content_hash({"key": key})[:24]
    created = _now_ms() if now_ms is None else _check_ms(now_ms, "now_ms")
    record = {
        **body,
        "record_id": record_id,
        "idempotency_key": key,
        "created_at": created,
    }
    record["record_hash"] = C.content_hash(record)
    size = len(C.canonical_json(record).encode("utf-8"))
    if size > MAX_RECORD_BYTES:
        raise EvidenceStoreError(
            f"record is {size} bytes; the bound is {MAX_RECORD_BYTES}",
            code="budget_exceeded")
    return record


def _validate_record(record: Any, *, line: int) -> dict[str, Any]:
    where = f"line {line}"
    if not isinstance(record, Mapping):
        raise EvidenceStoreError(f"{where} is not an object")
    unknown = sorted(set(record) - _TOP_FIELDS)
    if unknown:
        raise EvidenceStoreError(
            f"{where} has unknown fields: {unknown}", code="unknown_field")
    if record.get("schema") != STORE_NAMESPACE:
        raise EvidenceStoreError(
            f"{where} has schema {record.get('schema')!r}",
            code="unsupported_version")
    if record.get("record_version") != RECORD_VERSION:
        raise EvidenceStoreError(
            f"{where} has record version {record.get('record_version')!r}",
            code="unsupported_version")
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id):
        raise EvidenceStoreError(f"{where} has an invalid record_id")
    key = record.get("idempotency_key")
    if not isinstance(key, str) or not IDEMPOTENCY_RE.fullmatch(key):
        raise EvidenceStoreError(f"{where} has an invalid idempotency_key")
    if record_id != "ev-" + C.content_hash({"key": key})[:24]:
        raise EvidenceStoreError(f"{where} record_id does not match its key")
    if record.get("kind") not in RECORD_KINDS:
        raise EvidenceStoreError(f"{where} has an invalid kind")
    source = record.get("source")
    if not isinstance(source, str) or not source or len(source) > 120:
        raise EvidenceStoreError(f"{where} has an invalid source")
    observed_at = record.get("observed_at")
    if not isinstance(observed_at, (int, str)) or isinstance(observed_at, bool):
        raise EvidenceStoreError(f"{where} has an invalid observed_at")
    if not isinstance(record.get("created_at"), int) or isinstance(
            record.get("created_at"), bool):
        raise EvidenceStoreError(f"{where} has an invalid created_at")
    if record.get("evidence") not in RECORD_EVIDENCE:
        raise EvidenceStoreError(f"{where} has invalid evidence")
    if record.get("freshness") not in C.FRESHNESS:
        raise EvidenceStoreError(f"{where} has invalid freshness")
    if record.get("complete") not in (True, False):
        raise EvidenceStoreError(f"{where} has invalid complete")
    reason = record.get("reason")
    if reason is not None and (not isinstance(reason, str)
                               or len(reason) > MAX_REASON):
        raise EvidenceStoreError(f"{where} has an invalid reason")
    if record.get("kind") == "declaration" and (
            record.get("evidence") != "declared"
            or record.get("freshness") != "unknown"):
        raise EvidenceStoreError(
            f"{where} upgrades a declaration to evidence")
    digest = record.get("record_hash")
    if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
        raise EvidenceStoreError(f"{where} has no record hash")
    expected = C.content_hash({name: value for name, value in record.items()
                               if name != "record_hash"})
    if expected != digest:
        raise EvidenceStoreError(f"{where} hash does not match its contents")
    # Re-normalising the stored sections also proves their types and bounds.
    goal = _goal_ref(record["goal"]) if record.get("goal") is not None else None
    runs = _run_refs(record.get("runs") or [])
    tests = _test_refs(record.get("tests") or [])
    criteria = _criteria_refs(record.get("criteria") or [])
    environments = _environment_refs(record.get("environments") or [])
    assessment = (_assessment_ref(record["assessment"])
                  if record.get("assessment") is not None else None)
    declaration = (_declaration_ref(record["declaration"])
                   if record.get("declaration") is not None else None)
    verification = _verification_ref(record.get("verification"))
    _refs(record.get("refs") or {})
    if record["kind"] == "declaration" and verification["state"] != "unverified":
        raise EvidenceStoreError(
            f"{where} declaration carries a verified verification")
    if (record["evidence"] == "verified"
            and verification["state"] != "verified") or (
            record["evidence"] == "observed"
            and verification["state"] == "verified"):
        raise EvidenceStoreError(
            f"{where} evidence and verification disagree")
    if record["kind"] == "assessment" and assessment is not None:
        expected_evidence = "verified" if assessment["green"] else "observed"
        expected_freshness = (
            "current" if assessment["status"] in ("green", "failed")
            else "unknown")
        if (record["evidence"] != expected_evidence
                or record["freshness"] != expected_freshness):
            raise EvidenceStoreError(
                f"{where} assessment evidence/freshness disagree")
    if record.get("subject") != _subject(
            record["kind"], goal, runs, tests, declaration):
        raise EvidenceStoreError(f"{where} subject does not match its refs")
    if record["kind"] == "declaration" and declaration is None:
        raise EvidenceStoreError(f"{where} declaration has no declaration")
    if record["kind"] == "declaration":
        if assessment is not None or criteria or environments:
            raise EvidenceStoreError(
                f"{where} declaration carries observed sections")
        if any(run.get("state") != "unknown" or run.get("status")
               or run.get("digest") or run.get("output_digest")
               for run in runs):
            raise EvidenceStoreError(
                f"{where} declaration carries observed run results")
        if any(test.get("outcome") != "unknown"
               or test.get("recorded_at") is not None
               or test.get("run") is not None for test in tests):
            raise EvidenceStoreError(
                f"{where} declaration carries observed test results")
    if record["kind"] == "assessment" and assessment is None:
        raise EvidenceStoreError(f"{where} assessment has no assessment")
    if record["kind"] == "observation" and (assessment is not None
                                            or declaration is not None):
        raise EvidenceStoreError(f"{where} observation has a foreign section")
    return dict(record)


# ---------------------------------------------------------------------------
# Journal read, recovery and atomic writes
# ---------------------------------------------------------------------------


def store_dir(root: str | Path) -> Path:
    from ozagia.config import state_path

    return state_path(root, EVIDENCE_DIR)


def journal_path(root: str | Path) -> Path:
    return store_dir(root) / JOURNAL_NAME


def _resolve_root(root: str | Path) -> Path:
    try:
        path = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvidenceStoreError(
            f"repository root does not exist: {root}") from exc
    if not path.is_dir():
        raise EvidenceStoreError(f"repository root is not a directory: {path}")
    return path


def _parse_journal(path: Path) -> dict[str, Any]:
    """Scan the journal without writing. Fail closed on interior damage."""
    result: dict[str, Any] = {
        "state": "absent", "records": [], "partial": [], "reasons": [],
        "tail_record": None,
    }
    try:
        info = path.stat()
    except FileNotFoundError:
        return result
    except OSError as exc:
        result.update(state="invalid",
                      reasons=[f"cannot stat the evidence journal: {exc}"])
        return result
    if info.st_size > MAX_STORE_BYTES:
        result.update(
            state="invalid",
            reasons=[f"the evidence journal is {info.st_size} bytes; the "
                     f"bound is {MAX_STORE_BYTES}"])
        return result
    try:
        data = path.read_bytes()
    except OSError as exc:
        result.update(state="invalid",
                      reasons=[f"cannot read the evidence journal: {exc}"])
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        result.update(state="invalid",
                      reasons=[f"the evidence journal is not UTF-8: {exc}"])
        return result
    if not text:
        result["state"] = "current"
        return result

    lines = text.split("\n")
    unterminated = not text.endswith("\n")
    if not unterminated:
        lines.pop()  # the empty remainder after the final newline
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        last = index == len(lines)
        if not line:
            result.update(
                state="invalid",
                reasons=[f"line {index} of the evidence journal is empty"])
            return result
        if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            message = (f"line {index} is larger than {MAX_RECORD_BYTES} bytes")
            if last and unterminated:
                result["partial"].append({"line": index, "reason": message})
                result["records"] = records
                result["state"] = "partial"
                return result
            result.update(state="invalid", reasons=[message])
            return result
        try:
            raw = json.loads(line)
        except ValueError as exc:
            message = f"line {index} is not valid JSON: {exc}"
            if last and unterminated:
                result["partial"].append({"line": index, "reason": message})
                result["records"] = records
                result["state"] = "partial"
                return result
            result.update(state="invalid", reasons=[message])
            return result
        try:
            record = _validate_record(raw, line=index)
        except EvidenceStoreError as exc:
            message = f"line {index}: {exc}"
            if exc.code == "unsupported_version":
                if last and unterminated:
                    result["partial"].append(
                        {"line": index, "reason": message})
                    result["records"] = records
                    result["state"] = "partial"
                    return result
                result.update(state="unsupported", reasons=[message])
                return result
            if last and unterminated:
                result["partial"].append({"line": index, "reason": message})
                result["records"] = records
                result["state"] = "partial"
                return result
            result.update(state="invalid", reasons=[message])
            return result
        if last and unterminated:
            # Complete JSON, but not framed as a finished write line.
            result["tail_record"] = record
            result["partial"].append({
                "line": index,
                "reason": "the last line has no newline terminator",
            })
            result["records"] = records
            result["state"] = "partial"
            return result
        records.append(record)
    result["records"] = records
    result["state"] = "current"
    return result


def _canonical_lines(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(C.canonical_json(dict(record)) + "\n"
                   for record in records).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _append_line(path: Path, record: Mapping[str, Any]) -> None:
    data = (C.canonical_json(dict(record)) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        raise EvidenceStoreError(
            "the evidence store needs a file lock (fcntl), which this "
            "platform does not have", code="unavailable") from None
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    with _THREAD_LOCK, open(directory / LOCK_NAME, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EvidenceStoreError(
                        f"the evidence store is locked by another writer "
                        f"(waited {LOCK_TIMEOUT_S:.0f} s)",
                        code="unavailable") from None
                time.sleep(0.05)
            except OSError as exc:
                raise EvidenceStoreError(
                    f"cannot lock the evidence store: {exc}",
                    code="unavailable") from None
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def _recover_locked(path: Path, parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite a torn tail into complete lines; return the re-parsed journal."""
    records = list(parsed["records"])
    tail = parsed.get("tail_record")
    if isinstance(tail, Mapping):
        records.append(dict(tail))
    _atomic_write(path, _canonical_lines(records))
    return _parse_journal(path)


def _event_hash(record: Mapping[str, Any]) -> str:
    return C.content_hash({
        key: value for key, value in record.items()
        if key not in ("record_id", "created_at", "idempotency_key",
                       "record_hash")
    })


def _compact_locked(path: Path, parsed: Mapping[str, Any], *,
                    max_records: int, max_age_ms: int, now_ms: int,
                    keep_id: str | None = None) -> dict[str, Any]:
    records = list(parsed["records"])
    before = len(records)
    ordered = sorted(
        records,
        key=lambda item: (int(item.get("created_at") or 0),
                          str(item.get("record_id") or "")),
        reverse=True)
    kept: list[dict[str, Any]] = []
    for record in ordered:
        if len(kept) >= max_records:
            break
        age = now_ms - int(record.get("created_at") or 0)
        if max_age_ms and age > max_age_ms:
            continue
        kept.append(dict(record))
    if keep_id is not None and not any(
            item.get("record_id") == keep_id for item in kept):
        fresh = next((dict(item) for item in ordered
                      if item.get("record_id") == keep_id), None)
        if fresh is not None:
            kept = kept[:max(0, max_records - 1)] + [fresh]
    kept.sort(key=lambda item: (int(item.get("created_at") or 0),
                                str(item.get("record_id") or "")))
    _atomic_write(path, _canonical_lines(kept))
    return {
        "state": "current",
        "before": before,
        "after": len(kept),
        "evicted": before - len(kept),
        "path": path.as_posix(),
    }


def _needs_compaction(path: Path, records: Sequence[Mapping[str, Any]], *,
                      max_records: int, max_age_ms: int,
                      now_ms: int) -> bool:
    if len(records) > max_records:
        return True
    if max_age_ms:
        oldest = min((int(item.get("created_at") or 0) for item in records),
                     default=now_ms)
        if now_ms - oldest > max_age_ms:
            return True
    try:
        return path.stat().st_size > MAX_STORE_BYTES
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def append_record(root: str | Path, record: Mapping[str, Any], *,
                  now_ms: int | None = None,
                  idempotency_key: str | None = None,
                  max_records: int = MAX_RECORDS,
                  max_age_ms: int = MAX_AGE_MS) -> dict[str, Any]:
    """Validate and durably append one record; duplicate keys are idempotent."""
    root_path = _resolve_root(root)
    count_limit = _check_limit(max_records, "max_records", MAX_RECORDS)
    age_limit = _check_age(max_age_ms)
    created = _now_ms() if now_ms is None else _check_ms(now_ms, "now_ms")
    normalized = _normalize_record(record, now_ms=created,
                                   idempotency_key=idempotency_key)
    path = journal_path(root_path)
    with _locked(store_dir(root_path)):
        parsed = _parse_journal(path)
        recovered = False
        if parsed["state"] == "partial":
            parsed = _recover_locked(path, parsed)
            recovered = True
        if parsed["state"] in ("invalid", "unsupported"):
            raise EvidenceStoreError(
                f"the evidence journal is {parsed['state']}: "
                + "; ".join(parsed.get("reasons") or []),
                code="invalid_state")
        records = list(parsed["records"])
        newest = max((int(item.get("created_at") or 0) for item in records),
                     default=-1)
        if created <= newest:
            created = newest + 1
            normalized = _normalize_record(record, now_ms=created,
                                           idempotency_key=idempotency_key)
        for existing in records:
            if existing.get("idempotency_key") != normalized["idempotency_key"]:
                continue
            if _event_hash(existing) == _event_hash(normalized):
                return {
                    "state": "duplicate",
                    "duplicate": True,
                    "record": existing,
                    "path": path.as_posix(),
                    "recovered": recovered,
                    "retention": None,
                }
            raise EvidenceStoreError(
                f"idempotency key {normalized['idempotency_key']!r} is "
                f"already used by a different record", code="conflict")
        _append_line(path, normalized)
        records.append(normalized)
        retention = None
        if _needs_compaction(path, records, max_records=count_limit,
                             max_age_ms=age_limit, now_ms=created):
            retention = _compact_locked(
                path, {"records": records}, max_records=count_limit,
                max_age_ms=age_limit, now_ms=created,
                keep_id=normalized["record_id"])
        return {
            "state": "appended",
            "duplicate": False,
            "record": normalized,
            "path": path.as_posix(),
            "recovered": recovered,
            "retention": retention,
        }


def record_observation(
        root: str | Path, goal_id: str, *,
        run_ids: Iterable[str] = (),
        test_keys: Iterable[str] = (),
        sources: E.Sources | None = None,
        observed_at: int | str | None = None,
        now_ms: int | None = None,
        idempotency_key: str | None = None,
        max_records: int = MAX_RECORDS,
        max_age_ms: int = MAX_AGE_MS) -> dict[str, Any]:
    """Observe the read-only evidence join and store one durable record.

    Only facts read from the existing owners are stored.  The caller cannot
    supply a test outcome, criterion result or interpreter reference, so a
    model or plan statement cannot become observed evidence through this
    function.
    """
    report = E.collect_evidence(
        root, goal_id, run_ids=run_ids, test_keys=test_keys, sources=sources,
        observed_at="unknown" if observed_at is None else observed_at)
    goal = report["goal"]
    criteria = [_criterion_ref(item) for item in report["criteria"]]
    runs = [_run_ref(item) for item in report["runs"]]
    tests = [_test_ref(item) for item in report["test_keys"]]
    environments = [_environment_ref(item) for item in report["environment"]]
    refs = {"report_hash": report["hash"]}
    if isinstance(report["test_record"].get("digest"), str):
        refs["test_record_digest"] = report["test_record"]["digest"]
    observed = observed_at
    if observed is None:
        observed = _best_observed(report)
    verification = _verify_observation(
        goal_state=str(goal.get("state") or "unknown"),
        criteria=criteria, tests=tests, environments=environments)
    evidence = ("verified" if verification["state"] == "verified"
                else "observed")
    freshness = ("current" if goal.get("state") == "present"
                 and all(item["freshness"] in ("current", "absent")
                         for item in criteria)
                 else "unknown")
    record = {
        "kind": "observation",
        "source": "ozagia.core.evidence.collect_evidence",
        "observed_at": observed,
        "evidence": evidence,
        "freshness": freshness,
        "complete": True,
        "reason": None,
        "goal": {"id": goal["id"], "status": goal.get("status"),
                 "state": goal.get("state")},
        "criteria": criteria,
        "runs": runs,
        "tests": tests,
        "environments": environments,
        "verification": verification,
        "refs": refs,
    }
    return append_record(root, record, now_ms=now_ms,
                         idempotency_key=idempotency_key,
                         max_records=max_records, max_age_ms=max_age_ms)


def _best_observed(report: Mapping[str, Any]) -> int | str:
    values: list[int] = []
    for section in ("goal", "test_record"):
        item = report.get(section) or {}
        value = item.get("observed_at") if isinstance(item, Mapping) else None
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    for name in ("runs", "environment"):
        for item in report.get(name) or []:
            if isinstance(item, Mapping):
                value = item.get("observed_at")
                if isinstance(value, int) and not isinstance(value, bool):
                    values.append(value)
    return max(values) if values else "unknown"


def _criterion_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    test = item.get("test") if isinstance(item.get("test"), Mapping) else None
    key = test.get("key") if isinstance(test, Mapping) else None
    if not isinstance(key, str) or not key.startswith("test:"):
        args = item.get("args") if isinstance(item.get("args"), Mapping) else {}
        candidate = args.get("key") if isinstance(args, Mapping) else None
        key = candidate if isinstance(candidate, str) and candidate.startswith(
            "test:") else None
    return {
        "id": item.get("id"),
        "kind": item.get("kind"),
        "result": item.get("result"),
        "freshness": item.get("freshness"),
        "evidence": item.get("evidence"),
        "test_key": key,
        "recorded_status": item.get("recorded_status"),
    }


def _run_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item.get("run_id"),
        "state": item.get("state"),
        "status": item.get("status"),
        "digest": item.get("digest"),
        "output_digest": item.get("output_digest"),
    }


def _test_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    run = item.get("run") if isinstance(item.get("run"), Mapping) else None
    return {
        "key": item.get("key"),
        "outcome": item.get("outcome"),
        "recorded_at": item.get("recorded_at"),
        "run": ({"complete": bool(run.get("complete")),
                 "filtered": bool(run.get("filtered")),
                 "source": run.get("source")} if run else None),
    }


def _environment_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": item.get("path"),
        "source": item.get("source"),
        "project": bool(item.get("project")),
        "trusted": bool(item.get("trusted")),
        "exists": bool(item.get("exists")),
        "freshness": item.get("freshness"),
    }


def _verify_observation(*, goal_state: str,
                        criteria: Sequence[Mapping[str, Any]],
                        tests: Sequence[Mapping[str, Any]],
                        environments: Sequence[Mapping[str, Any]]
                        ) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    reasons: list[str] = []

    def check(name: str, ok: bool | None) -> None:
        checks.append({"name": name,
                       "state": ("passed" if ok is True else
                                 "failed" if ok is False else "unknown")})
        if ok is not True:
            reasons.append(name)

    check("goal_present", goal_state == "present")
    test_criteria = [item for item in criteria
                     if item.get("kind") in ("test_passes", "test_fails")]
    check("has_test_criterion", bool(test_criteria))
    check("all_criteria_test",
          bool(criteria) and len(test_criteria) == len(criteria))
    by_key = {item["key"]: item for item in tests}
    for criterion in test_criteria:
        expected_outcome = ("passed" if criterion["kind"] == "test_passes"
                            else "failed")
        key = criterion.get("test_key")
        recorded = by_key.get(key)
        check(f"criterion_{criterion['id']}_current",
              criterion.get("freshness") == "current")
        check(f"criterion_{criterion['id']}_result",
              criterion.get("result") == "passed")
        check(f"test_{key}_recorded",
              recorded is not None if key else False)
        if recorded is None:
            continue
        check(f"test_{key}_outcome", recorded.get("outcome") == expected_outcome)
        run = recorded.get("run") if isinstance(recorded.get("run"),
                                                Mapping) else None
        check(f"test_{key}_run_complete", bool(run and run.get("complete")))
        check(f"test_{key}_run_unfiltered",
              bool(run) and not bool(run.get("filtered")))
    usable = [item for item in environments
              if item.get("exists") is True and item.get("trusted") is True]
    check("trusted_interpreter", bool(usable))
    state = "verified" if all(item["state"] == "passed"
                              for item in checks) else "unverified"
    return {
        "state": state,
        "method": "recorded_test_goal_join",
        "checks": checks,
        "reasons": reasons[:MAX_REASONS],
    }


def record_partial(
        root: str | Path, *, source: str = "ozagia.core.evidence_store",
        reason: str, goal_id: str | None = None,
        run_id: str | None = None, test_key: str | None = None,
        now_ms: int | None = None,
        idempotency_key: str | None = None,
        max_records: int = MAX_RECORDS,
        max_age_ms: int = MAX_AGE_MS) -> dict[str, Any]:
    """Store an explicitly incomplete record (for example, a crashed run).

    It is durable and inspectable, but ``complete=false`` and
    ``freshness=unknown``: readers list it under ``partial`` and never serve
    it as current.  A later complete observation with the same subject
    supersedes it.
    """
    goal = {"id": _goal_identity(goal_id)} if goal_id is not None else None
    runs = []
    if run_id is not None:
        runs = [{"run_id": _run_filter(run_id), "state": "unknown",
                 "status": None, "digest": None, "output_digest": None}]
    tests = []
    if test_key is not None:
        if not isinstance(test_key, str) or not TEST_KEY_RE.fullmatch(test_key):
            raise EvidenceStoreError("test_key is not a test key")
        tests = [{"key": test_key, "outcome": "unknown",
                  "recorded_at": None, "run": None}]
    record = {
        "kind": "observation",
        "source": _source(source),
        "observed_at": "unknown",
        "evidence": "observed",
        "freshness": "unknown",
        "complete": False,
        "reason": _redacted(reason, MAX_REASON) or "an incomplete record",
        "goal": goal,
        "runs": runs,
        "tests": tests,
        "verification": {
            "state": "unverified",
            "method": None,
            "checks": [{"name": "record_complete", "state": "failed"}],
            "reasons": ["the record was written as incomplete"],
        },
        "refs": {},
    }
    return append_record(root, record, now_ms=now_ms,
                         idempotency_key=idempotency_key,
                         max_records=max_records, max_age_ms=max_age_ms)


def record_declaration(
        root: str | Path, *, source: str, statement: str,
        goal_id: str | None = None, run_id: str | None = None,
        test_key: str | None = None,
        refs: Mapping[str, Any] | None = None,
        now_ms: int | None = None,
        idempotency_key: str | None = None,
        max_records: int = MAX_RECORDS,
        max_age_ms: int = MAX_AGE_MS) -> dict[str, Any]:
    """Store a caller/model claim.  It is never evidence or current."""
    record: dict[str, Any] = {
        "kind": "declaration",
        "source": _source(source),
        "observed_at": "unknown",
        "evidence": "declared",
        "freshness": "unknown",
        "complete": True,
        "reason": None,
        "declaration": {"statement": statement},
        "refs": dict(refs or {}),
    }
    if goal_id is not None:
        record["goal"] = {"id": _goal_identity(goal_id),
                          "status": None, "state": "unknown"}
    if run_id is not None:
        record["runs"] = [{"run_id": _run_filter(run_id),
                           "state": "unknown", "status": None,
                           "digest": None, "output_digest": None}]
    if test_key is not None:
        if not isinstance(test_key, str) or not TEST_KEY_RE.fullmatch(test_key):
            raise EvidenceStoreError("test_key is not a test key")
        record["tests"] = [{"key": test_key, "outcome": "unknown",
                            "recorded_at": None, "run": None}]
    return append_record(root, record, now_ms=now_ms,
                         idempotency_key=idempotency_key,
                         max_records=max_records, max_age_ms=max_age_ms)


def record_assessment(
        root: str | Path, report: Mapping[str, Any], *,
        now_ms: int | None = None,
        idempotency_key: str | None = None,
        max_records: int = MAX_RECORDS,
        max_age_ms: int = MAX_AGE_MS) -> dict[str, Any]:
    """Store a bounded link to a host-produced preflight assessment.

    The report hash is checked when present.  A green assessment is refused
    unless every required dimension is ``true`` and it carries no non-true
    reasons, so a missing interpreter or stale graph cannot be written as
    green merely by changing the outer status.
    """
    if not isinstance(report, Mapping):
        raise EvidenceStoreError("a preflight report must be a mapping")
    digest = report.get("hash")
    if digest is not None:
        expected = C.content_hash({key: value for key, value in report.items()
                                   if key != "hash"})
        if digest != expected:
            raise EvidenceStoreError(
                "preflight report hash does not match its contents",
                code="invalid_value")
    schema = report.get("schema")
    if not isinstance(schema, Mapping) or schema.get(
            "namespace") != "ozagia.task_preflight":
        raise EvidenceStoreError(
            "assessment reports must be ozagia.task_preflight reports",
            code="invalid_value")
    if schema.get("version") != "1.0":
        raise EvidenceStoreError(
            f"preflight report version {schema.get('version')!r} is not "
            "supported", code="unsupported_version")
    goal = report.get("goal")
    if not isinstance(goal, Mapping) or goal.get("id") is None:
        raise EvidenceStoreError("the preflight report has no goal")
    assessment = report.get("assessment")
    if not isinstance(assessment, Mapping):
        raise EvidenceStoreError("the preflight report has no assessment")
    dimensions = report.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise EvidenceStoreError("the preflight report has no dimensions")
    dim_input: dict[str, Any] = {}
    for name in ASSESSMENT_DIMENSIONS:
        row = dimensions.get(name)
        if not isinstance(row, Mapping):
            raise EvidenceStoreError(
                f"the preflight report is missing dimension {name!r}",
                code="invalid_value")
        dim_input[name] = {"readiness": row.get("readiness"),
                           "reason": row.get("reason")}
    checkouts = report.get("checkouts")
    environment = None
    if isinstance(checkouts, list) and checkouts:
        primary = checkouts[0] if isinstance(checkouts[0], Mapping) else {}
        rows = primary.get("dimensions") if isinstance(primary, Mapping) else {}
        env = rows.get("environment") if isinstance(rows, Mapping) else {}
        data = env.get("data") if isinstance(env, Mapping) else {}
        if isinstance(data, Mapping):
            candidates = data.get("candidates")
            if isinstance(candidates, list):
                candidates = len(candidates)
            elif not isinstance(candidates, int) or isinstance(candidates, bool):
                candidates = 0
            environment = {
                "selected": data.get("selected"),
                "candidates": candidates,
                "readiness": dim_input["environment"]["readiness"],
                "reason": dim_input["environment"]["reason"],
            }
    criteria = report.get("criteria")
    criteria_input = None
    if isinstance(criteria, Mapping):
        criteria_input = {
            "total": criteria.get("total", 0),
            "counts": criteria.get("counts", {}),
        }
    status = assessment.get("status")
    green = assessment.get("green")
    reasons = assessment.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    reasons = [item.get("reason") if isinstance(item, Mapping) else item
               for item in reasons]
    record = {
        "kind": "assessment",
        "source": "ozagia.core.task_preflight.build_preflight",
        "observed_at": report.get("started_at", "unknown"),
        "evidence": "observed",
        "freshness": ("current" if status in ("green", "failed")
                      else "unknown"),
        "complete": True,
        "reason": None,
        "goal": {"id": goal.get("id"), "status": goal.get("status"),
                 "state": "present"},
        "assessment": {
            "status": status,
            "green": green,
            "dimensions": dim_input,
            "criteria": criteria_input,
            "reasons": reasons,
            "environment": environment,
        },
        "verification": {
            "state": "verified" if green is True else "unverified",
            "method": "task_preflight_assessment",
            "checks": [{
                "name": "required_dimensions",
                "state": "passed" if green is True else "failed",
            }],
            "reasons": [] if green is True else [
                "the assessment is not green"],
        },
        "refs": {"preflight_hash": digest} if digest is not None else {},
    }
    return append_record(root, record, now_ms=now_ms,
                         idempotency_key=idempotency_key,
                         max_records=max_records, max_age_ms=max_age_ms)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def _decorate(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Newest-first copies with view-only ``current`` and supersession."""
    ordered = sorted(
        (dict(item) for item in records),
        key=lambda item: (int(item.get("created_at") or 0),
                          str(item.get("record_id") or "")),
        reverse=True)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in ordered:
        subject = str(record.get("subject") or "")
        complete = record.get("complete") is True
        current = bool(
            complete
            and record.get("freshness") == "current"
            and record.get("evidence") != "declared"
            and subject not in seen)
        seen.add(subject)
        record["current"] = current
        out.append(record)
    return out


def _matches(record: Mapping[str, Any], *, kind: str | None,
             goal: str | None, run: str | None) -> bool:
    if kind is not None and record.get("kind") != kind:
        return False
    if goal is not None:
        row = record.get("goal")
        if not isinstance(row, Mapping) or row.get("id") != goal:
            return False
    if run is not None:
        rows = record.get("runs")
        if not isinstance(rows, list) or not any(
                isinstance(item, Mapping) and item.get("run_id") == run
                for item in rows):
            return False
    return True


def read_store(root: str | Path, *, limit: int = MAX_RECORDS,
               kind: str | None = None, goal: str | None = None,
               run: str | None = None, include_partial: bool = False
               ) -> dict[str, Any]:
    """Read the bounded store; partial records are never served as current."""
    root_path = _resolve_root(root)
    count_limit = _check_limit(limit, "limit", MAX_RECORDS)
    if kind is not None and kind not in RECORD_KINDS:
        raise EvidenceStoreError(
            f"kind must be one of {list(RECORD_KINDS)}")
    goal_id = _goal_filter(goal) if goal is not None else None
    run_id = _run_filter(run) if run is not None else None
    path = journal_path(root_path)
    parsed = _parse_journal(path)
    view: dict[str, Any] = {
        "schema": {"namespace": STORE_NAMESPACE, "version": STORE_VERSION},
        "state": parsed["state"],
        "path": path.as_posix(),
        "observed_at": "unknown",
        "records": [],
        "partial": [],
        "counts": {"total": 0, "partial": 0, "matched": 0, "returned": 0,
                   "current": 0},
        "truncated": C.truncation().to_dict(),
        "reasons": list(parsed.get("reasons") or []),
        "unknowns": [
            "the store proves recorded references, not that a test re-ran",
            "the test record does not record which interpreter ran a test",
            "a task run is not linked to a test-record run by the owners",
            "only runs named by the observer are joined",
            "evidence retention can evict old records",
        ],
    }
    decorated = _decorate(parsed["records"])
    view["counts"]["total"] = sum(
        1 for item in decorated if item.get("complete") is True)
    view["counts"]["partial"] = len(parsed["partial"]) + sum(
        1 for item in decorated if item.get("complete") is not True)
    observed = [item.get("observed_at") for item in decorated
                if isinstance(item.get("observed_at"), int)
                and not isinstance(item.get("observed_at"), bool)]
    if observed:
        view["observed_at"] = max(observed)
    for record in decorated:
        if record.get("complete") is True:
            continue
        if include_partial:
            view["partial"].append({
                "line": None,
                "reason": record.get("reason"),
                "record": {**record, "current": False},
            })
    for entry in parsed["partial"]:
        view["partial"].append(dict(entry))
    matched = [item for item in decorated
               if _matches(item, kind=kind, goal=goal_id, run=run_id)]
    view["counts"]["matched"] = len(matched)
    returned = matched[:count_limit]
    view["records"] = returned
    view["counts"]["returned"] = len(returned)
    view["counts"]["current"] = sum(
        1 for item in returned if item.get("current"))
    if len(matched) > len(returned):
        view["truncated"] = C.truncation(
            True, omitted=len(matched) - len(returned),
            reason="limit").to_dict()
    if parsed["state"] == "partial":
        view["reasons"] = [
            "the evidence journal has an incomplete final write; it is not "
            "served and the next append recovers it"] + view["reasons"]
    if parsed["state"] in ("invalid", "unsupported"):
        view["records"] = []
        view["counts"]["returned"] = 0
        view["counts"]["current"] = 0
        view["unknowns"].append(
            "the store failed closed; no record from it is current")
    return view


def latest_record(root: str | Path, *, kind: str | None = None,
                  goal: str | None = None, run: str | None = None,
                  current_only: bool = True,
                  include_partial: bool = False) -> dict[str, Any] | None:
    """The newest matching record; partial records only on explicit request."""
    view = read_store(root, limit=MAX_RECORDS, kind=kind, goal=goal, run=run,
                      include_partial=include_partial)
    records = view["records"]
    if current_only:
        records = [item for item in records if item.get("current")]
    if records:
        return records[0]
    if include_partial:
        for entry in view["partial"]:
            record = entry.get("record")
            if isinstance(record, Mapping):
                return {**record, "current": False, "partial": True}
    return None


def compact_store(root: str | Path, *, max_records: int = MAX_RECORDS,
                  max_age_ms: int = MAX_AGE_MS,
                  now_ms: int | None = None) -> dict[str, Any]:
    """Evict old records under the lock; never leaves a torn tail behind."""
    root_path = _resolve_root(root)
    count_limit = _check_limit(max_records, "max_records", MAX_RECORDS)
    age_limit = _check_age(max_age_ms)
    now = _now_ms() if now_ms is None else _check_ms(now_ms, "now_ms")
    path = journal_path(root_path)
    with _locked(store_dir(root_path)):
        parsed = _parse_journal(path)
        if parsed["state"] == "absent":
            return {"state": "absent", "before": 0, "after": 0,
                    "evicted": 0, "path": path.as_posix()}
        if parsed["state"] == "partial":
            parsed = _recover_locked(path, parsed)
        if parsed["state"] in ("invalid", "unsupported"):
            raise EvidenceStoreError(
                f"the evidence journal is {parsed['state']}: "
                + "; ".join(parsed.get("reasons") or []),
                code="invalid_state")
        return _compact_locked(path, parsed, max_records=count_limit,
                               max_age_ms=age_limit, now_ms=now)


def store_summary(root: str | Path) -> dict[str, Any]:
    """A small, bounded status view for callers that only need counts."""
    view = read_store(root, limit=1)
    return {
        "schema": view["schema"],
        "state": view["state"],
        "path": view["path"],
        "observed_at": view["observed_at"],
        "counts": view["counts"],
        "reasons": view["reasons"],
    }


__all__ = [
    "ASSESSMENTS", "ASSESSMENT_DIMENSIONS", "EvidenceStoreError",
    "EVIDENCE_DIR", "JOURNAL_NAME", "LOCK_NAME", "MAX_AGE_MS", "MAX_RECORDS",
    "MAX_RECORD_BYTES", "MAX_STORE_BYTES", "RECORD_EVIDENCE", "RECORD_KINDS",
    "RECORD_VERSION", "STORE_NAMESPACE", "STORE_STATES", "STORE_VERSION",
    "append_record", "compact_store", "journal_path", "latest_record",
    "read_store", "record_assessment", "record_declaration",
    "record_observation", "record_partial", "store_dir", "store_summary",
]
