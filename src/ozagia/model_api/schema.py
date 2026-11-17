"""Version-1 request, map-schema, instance and plain-rendering contracts.

The M3 model API is a *projection* boundary.  This module owns the shapes and
hashes at that boundary and reuses :mod:`ozagia.core.contracts` for canonical
JSON, schema fingerprints, identity validation, instance hashing, fact
freshness/evidence vocabulary and truncation envelopes.  It performs no I/O.

Canonical decisions frozen here
-------------------------------

* Canonical JSON is the shared v1 form (sorted keys, UTF-8, no floats, ints in
  the signed 64-bit range).  Text/protocol encodings are renderers only.
* A map schema is a normalised v1 schema record with namespace
  ``ozagia.model_api.<map>`` and version ``1.0``.  Its fingerprint is
  :func:`ozagia.core.contracts.schema_fingerprint` over exactly namespace,
  version, types, required, constraints and sensitivity.
* A map instance is a normalised v1 instance record.  Its content hash is
  :func:`ozagia.core.contracts.instance_hash`: ``created_at``, ``cursor`` and
  ``trace_id`` are transport-only and never enter the hash.
* A map page is capped by caller budgets, never above fixed hard bounds.  The
  truncation marker is the shared ``{truncated, omitted, next_cursor, reason}``
  envelope, stored on the instance so a cached page carries its own omissions.
* Every map data record carries source identities, scope, snapshot/input
  identities, freshness, evidence, trust, omissions and truncation.
* Plain (L0-style) output is rendered from the same fact list as the
  structured view.  It is not a second authority and does not add facts.

This module does not decide support, permission or freshness by itself; the
projectors inject observed values and host policy remains authoritative.
"""

from __future__ import annotations

import copy
import re
from functools import lru_cache
from typing import Any, Iterable, Mapping

from ozagia.core import contracts as C

REQUEST_NAMESPACE = "ozagia.model_api.request"
RESPONSE_NAMESPACE = "ozagia.model_api.response"
MAP_NAMESPACE_PREFIX = "ozagia.model_api."
MAP_VERSION = "1.0"
REQUEST_VERSION = "1.0"

MAP_TYPES = ("capabilities", "graph", "outline", "work")
MAP_LABELS = {
    "capabilities": "declared capabilities and observed surfaces",
    "graph": "bounded repository graph slice",
    "outline": "bounded structural outline",
    "work": "goal/work/evidence records",
}
FORMATS = ("plain", "structured")
MAP_STATES = ("ok", "degraded", "absent", "unknown")
TRUST_LABELS = ("untrusted-data", "host-observed")
ITEM_KINDS = (
    "capability", "criterion", "environment", "goal", "node", "outline",
    "relation", "run", "test", "test_record",
)

DEFAULT_PAGE_ITEMS = 25
HARD_MAX_ITEMS = 200
HARD_MAX_BYTES = 256 * 1024
HARD_MAX_DEPTH = 4
HARD_MAX_CURSOR_OFFSET = 1_000_000
MAX_TRACE_ID = 128
MAX_TARGETS = 8
MAX_RUNS = 8
MAX_TESTS = 16
MAX_SCOPE_PATHS = 32
MAX_LABEL = 128
MAX_REPO_PATH = 512

DEFAULT_BUDGETS = {
    "max_bytes": C.MAP_DEFAULT_MAX_BYTES,
    "max_items": DEFAULT_PAGE_ITEMS,
    "max_depth": C.MAP_DEFAULT_MAX_DEPTH,
}
HARD_BUDGETS = {
    "max_bytes": HARD_MAX_BYTES,
    "max_items": HARD_MAX_ITEMS,
    "max_depth": HARD_MAX_DEPTH,
}

_SCOPE_FIELDS = ("root", "repo", "checkout", "snapshot", "session", "model",
                 "paths")
_REQUEST_FIELDS = (
    "schema", "request_version", "map", "scope", "target", "budgets",
    "cursor", "trace_id", "format", "strict",
)
_TARGET_FIELDS = {
    "capabilities": ("capability",),
    "graph": ("key", "path", "type"),
    "outline": ("target", "targets"),
    "work": ("goal", "runs", "tests"),
}
_DATA_REQUIRED = (
    "bounds", "counts", "freshness", "items", "map", "relations",
    "source_identities", "state", "trust",
)
_DATA_OPTIONAL = {
    "capabilities": ("audit", "coverage", "not_assessed", "notices"),
    "graph": ("notices",),
    "outline": ("notices",),
    "work": ("coverage", "deferred", "not_assessed", "notices"),
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TRACE_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
_CURSOR_RE = re.compile(
    r"^v1:(?P<offset>0|[1-9][0-9]{0,8}):(?P<digest>[0-9a-f]{12})$")
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_TEST_RE = re.compile(r"^test:[^\s]{1,299}$")
_GOAL_RE = re.compile(r"^goal:[a-z0-9][a-z0-9._-]{0,127}$")


# ---------------------------------------------------------------------------
# Schema records
# ---------------------------------------------------------------------------


def _type_definition(fields: Mapping[str, str]) -> dict[str, Any]:
    return {
        "fields": dict(sorted(fields.items())),
        "additional_fields": "x-",
    }


def _schema_record(map_type: str) -> dict[str, Any]:
    optional = _DATA_OPTIONAL.get(map_type, ())
    types = {
        "source_identity": _type_definition({
            "id": "string|null", "state": "observed|declared|unknown|absent",
            "hash": "sha256|string|null", "source": "string",
        }),
        "item": _type_definition({
            "id": "bounded-string", "kind": "vocabulary",
            "freshness": "current|stale|unknown|absent",
            "evidence": "declared|inferred|observed|verified",
            "source": "string", "trust": "untrusted-data|host-observed",
            "summary": "bounded-string|null",
        }),
        "relation": _type_definition({
            "id": "bounded-string", "type": "string",
            "from": "bounded-string", "to": "bounded-string",
            "freshness": "vocabulary", "evidence": "vocabulary",
        }),
        "truncation": _type_definition({
            "truncated": "bool", "omitted": "non-negative-int",
            "next_cursor": "opaque|null", "reason": "vocabulary|null",
        }),
    }
    return {
        "namespace": f"{MAP_NAMESPACE_PREFIX}{map_type}",
        "version": MAP_VERSION,
        "types": types,
        "required": list(_DATA_REQUIRED),
        "constraints": {
            "max_bytes": HARD_MAX_BYTES,
            "max_items": HARD_MAX_ITEMS,
            "max_depth": HARD_MAX_DEPTH,
            "unknown_fields": "error",
            "optional_data_fields": list(optional),
            "sensitivity": "internal",
        },
        "sensitivity": "internal",
    }


@lru_cache(maxsize=len(MAP_TYPES))
def _map_schema_cached(map_type: str) -> dict[str, Any]:
    return C.validate_schema(_schema_record(map_type))


def map_schema(map_type: str) -> dict[str, Any]:
    """The normalised, validated v1 schema for one map type."""
    if map_type not in MAP_TYPES:
        raise ValueError(f"unknown map type {map_type!r}")
    return copy.deepcopy(_map_schema_cached(map_type))


@lru_cache(maxsize=len(MAP_TYPES))
def map_schema_fingerprint(map_type: str) -> str:
    return C.schema_fingerprint(map_schema(map_type))


def schema_ref(map_type: str) -> dict[str, str]:
    return {"namespace": f"{MAP_NAMESPACE_PREFIX}{map_type}",
            "version": MAP_VERSION}


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


def _clean_text(value: Any, *, name: str, limit: int,
                allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string", "invalid_value",
                    "malformed_request")
    text = value.strip()
    if not text and not allow_empty:
        raise _fail(f"{name} must not be empty", "invalid_value",
                    "malformed_request")
    if len(text) > limit:
        raise _fail(f"{name} exceeds {limit} characters", "invalid_value",
                    "malformed_request")
    if _CONTROL_RE.search(text):
        raise _fail(f"{name} contains control characters", "invalid_value",
                    "malformed_request")
    return text


def _label(value: Any, name: str) -> str:
    text = _clean_text(value, name=name, limit=MAX_LABEL)
    if not _LABEL_RE.fullmatch(text):
        raise _fail(f"{name} is not a bounded label", "invalid_value",
                    "malformed_request")
    return text


def _int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{name} must be an integer", "invalid_value",
                    "malformed_request")
    if value < minimum or value > maximum:
        raise _fail(f"{name} must be between {minimum} and {maximum}",
                    "budget_exceeded", "budget_over_hard_bound")
    return value


def _bool(value: Any, *, name: str) -> bool:
    if value is not True and value is not False:
        raise _fail(f"{name} must be a bool", "invalid_value",
                    "malformed_request")
    return value


def _fail(message: str, code: str, reason: str):
    # Imported lazily to keep the error module free of schema imports.
    from ozagia.model_api.errors import ModelAPIError

    return ModelAPIError(message, code=code, reason=reason)


def _unknown_fields(record: Mapping[str, Any], allowed: Iterable[str],
                    *, what: str) -> None:
    permitted = set(allowed)
    for key in record:
        if not isinstance(key, str):
            raise _fail(f"{what} keys must be strings", "invalid_request",
                        "malformed_request")
        if key in permitted or key.startswith(C.EXTENSION_PREFIX):
            continue
        raise _fail(f"{what} has unknown field {key!r}", "unknown_field",
                    "malformed_request")


def _normalise_path(value: Any, *, name: str) -> str:
    text = _clean_text(value, name=name, limit=MAX_REPO_PATH)
    if text.startswith("/") or text.startswith("\\"):
        raise _fail(f"{name} must be repository-relative", "forbidden",
                    "cross_scope")
    if re.match(r"^[A-Za-z]:[\\/]", text):
        raise _fail(f"{name} must be repository-relative", "forbidden",
                    "cross_scope")
    parts = text.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _fail(f"{name} is not a normalised relative path", "forbidden",
                    "cross_scope")
    return "/".join(parts)


def _id(value: Any, *, name: str, kind: str) -> str:
    text = _clean_text(value, name=name, limit=C.MAX_ID_LENGTH)
    try:
        parsed = C.parse_id(text)
    except C.ContractError as exc:
        raise _fail(f"{name} is not a {kind} identity", "invalid_request",
                    "malformed_scope") from exc
    if parsed.kind != kind:
        raise _fail(f"{name} must be a {kind} identity", "invalid_request",
                    "malformed_scope")
    return parsed.canonical


# ---------------------------------------------------------------------------
# Request contract
# ---------------------------------------------------------------------------


def make_request(map_type: str, *, scope: Mapping[str, Any] | None = None,
                 target: Mapping[str, Any] | None = None,
                 budgets: Mapping[str, Any] | None = None,
                 cursor: str | None = None, trace_id: str | None = None,
                 format: str = "structured", strict: bool = False) -> dict[str, Any]:
    """Build a canonical request mapping (validated by :func:`validate_request`)."""
    request: dict[str, Any] = {
        "schema": {"namespace": REQUEST_NAMESPACE, "version": REQUEST_VERSION},
        "request_version": REQUEST_VERSION,
        "map": map_type,
        "scope": dict(scope or {}),
        "target": dict(target or {}),
        "budgets": dict(budgets or {}),
        "format": format,
        "strict": strict,
    }
    if cursor is not None:
        request["cursor"] = cursor
    if trace_id is not None:
        request["trace_id"] = trace_id
    return validate_request(request)


def validate_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise one model-map request.

    Unknown non-``x-`` fields, floats-as-budgets, unknown map types, a wrong
    major schema version, cross-root path syntax and malformed targets are
    refused with typed errors.  Relative paths are normalised to POSIX form;
    absolute paths are never accepted from a request.
    """
    if not isinstance(raw, Mapping):
        raise _fail("request must be a mapping", "invalid_request",
                    "malformed_request")
    _unknown_fields(raw, _REQUEST_FIELDS, what="request")
    if "schema" not in raw:
        raise _fail("request is missing schema", "invalid_request",
                    "malformed_request")
    ref = raw["schema"]
    if not isinstance(ref, Mapping):
        raise _fail("request.schema must be a mapping", "invalid_request",
                    "malformed_request")
    _unknown_fields(ref, ("namespace", "version"), what="request.schema")
    if ref.get("namespace") != REQUEST_NAMESPACE:
        raise _fail("request schema namespace is not the model API request",
                    "invalid_request", "schema_mismatch")
    try:
        version = C.normalize_version(ref.get("version"))
    except C.ContractError as exc:
        raise _fail("request schema version is unsupported",
                    "unsupported_version", "unsupported_version") from exc
    if version != REQUEST_VERSION:
        raise _fail("request schema version is not 1.0", "unsupported_version",
                    "unsupported_version")
    if "request_version" in raw:
        try:
            request_version = C.normalize_version(raw["request_version"])
        except C.ContractError as exc:
            raise _fail("request_version is unsupported",
                        "unsupported_version", "unsupported_version") from exc
        if request_version != REQUEST_VERSION:
            raise _fail("request_version is not 1.0", "unsupported_version",
                        "unsupported_version")

    map_type = raw.get("map")
    if map_type not in MAP_TYPES:
        raise _fail(f"unknown map type {map_type!r}; expected one of "
                    f"{MAP_TYPES}", "unsupported", "unsupported_map")

    scope_raw = raw.get("scope", {})
    if not isinstance(scope_raw, Mapping):
        raise _fail("request.scope must be a mapping", "invalid_request",
                    "malformed_scope")
    _unknown_fields(scope_raw, _SCOPE_FIELDS, what="request.scope")
    scope: dict[str, Any] = {}
    if "root" in scope_raw:
        root = _clean_text(scope_raw["root"], name="scope.root",
                           limit=4096)
        if not root.startswith("/"):
            raise _fail("scope.root must be absolute", "invalid_request",
                        "malformed_scope")
        scope["root"] = root
    for name in ("repo", "checkout", "snapshot"):
        if name in scope_raw:
            scope[name] = _id(scope_raw[name], name=f"scope.{name}",
                              kind=name)
    for name in ("session", "model"):
        if name in scope_raw:
            scope[name] = _label(scope_raw[name], f"scope.{name}")
    if "paths" in scope_raw:
        raw_paths = scope_raw["paths"]
        if not isinstance(raw_paths, (list, tuple)) or isinstance(raw_paths, str):
            raise _fail("scope.paths must be a list", "invalid_request",
                        "malformed_scope")
        if len(raw_paths) > MAX_SCOPE_PATHS:
            raise _fail(f"scope.paths exceeds {MAX_SCOPE_PATHS} entries",
                        "budget_exceeded", "budget_over_hard_bound")
        paths = sorted({_normalise_path(item, name="scope.paths[]")
                        for item in raw_paths})
        if paths:
            scope["paths"] = paths

    target_raw = raw.get("target", {})
    if not isinstance(target_raw, Mapping):
        raise _fail("request.target must be a mapping", "invalid_request",
                    "malformed_target")
    _unknown_fields(target_raw, _TARGET_FIELDS[map_type], what="request.target")
    target = _validate_target(map_type, target_raw)

    budgets_raw = raw.get("budgets", {})
    if not isinstance(budgets_raw, Mapping):
        raise _fail("request.budgets must be a mapping", "invalid_request",
                    "malformed_budget")
    _unknown_fields(budgets_raw, tuple(DEFAULT_BUDGETS), what="request.budgets")
    budgets = dict(DEFAULT_BUDGETS)
    for name in DEFAULT_BUDGETS:
        if name in budgets_raw:
            budgets[name] = _int(
                budgets_raw[name], name=f"budgets.{name}", minimum=1,
                maximum=HARD_BUDGETS[name])
    if budgets["max_items"] > HARD_MAX_ITEMS:
        raise _fail("max_items exceeds the hard bound", "budget_exceeded",
                    "budget_over_hard_bound")

    cursor = raw.get("cursor")
    if cursor is not None:
        cursor = _clean_text(cursor, name="request.cursor", limit=256)
        decode_cursor(cursor)  # structural validation only

    trace_id = raw.get("trace_id")
    if trace_id is not None:
        trace_id = _clean_text(trace_id, name="request.trace_id",
                               limit=MAX_TRACE_ID)
        if not _TRACE_RE.fullmatch(trace_id):
            raise _fail("trace_id has invalid characters", "invalid_value",
                        "malformed_request")

    format_value = raw.get("format", "structured")
    if format_value not in FORMATS:
        raise _fail(f"unknown format {format_value!r}; expected one of "
                    f"{FORMATS}", "unsupported", "unsupported_format")

    strict = _bool(raw.get("strict", False), name="request.strict")

    return {
        "schema": {"namespace": REQUEST_NAMESPACE, "version": REQUEST_VERSION},
        "request_version": REQUEST_VERSION,
        "map": map_type,
        "scope": scope,
        "target": target,
        "budgets": budgets,
        "cursor": cursor,
        "trace_id": trace_id,
        "format": format_value,
        "strict": strict,
    }


def _validate_target(map_type: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    if map_type == "graph":
        if "key" in raw:
            target["key"] = _clean_text(raw["key"], name="target.key",
                                        limit=C.MAX_LOCAL_KEY_LENGTH)
            if _CONTROL_RE.search(target["key"]):
                raise _fail("target.key contains control characters",
                            "invalid_value", "malformed_target")
        if "path" in raw:
            target["path"] = _normalise_path(raw["path"], name="target.path")
        if "type" in raw:
            type_name = _clean_text(raw["type"], name="target.type", limit=64)
            if not _TYPE_RE.fullmatch(type_name):
                raise _fail("target.type is not a node type", "invalid_value",
                            "malformed_target")
            target["type"] = type_name
    elif map_type == "outline":
        if "target" in raw and "targets" in raw:
            raise _fail("give target or targets, not both", "invalid_request",
                        "malformed_target")
        if "target" in raw:
            target["targets"] = [
                _outline_target_text(raw["target"], "target.target")]
        elif "targets" in raw:
            values = raw["targets"]
            if not isinstance(values, (list, tuple)) or isinstance(values, str):
                raise _fail("target.targets must be a list", "invalid_request",
                            "malformed_target")
            if not values or len(values) > MAX_TARGETS:
                raise _fail(f"target.targets must have 1..{MAX_TARGETS} entries",
                            "invalid_request", "malformed_target")
            target["targets"] = [
                _outline_target_text(value, "target.targets[]")
                for value in values]
        else:
            raise _fail("an outline map needs target or targets",
                        "invalid_request", "malformed_target")
    elif map_type == "capabilities":
        if "capability" in raw:
            capability = _clean_text(raw["capability"],
                                     name="target.capability",
                                     limit=C.MAX_ID_LENGTH)
            try:
                parsed = C.parse_id(capability)
            except C.ContractError as exc:
                raise _fail("target.capability is not a cap: id",
                            "invalid_request", "malformed_target") from exc
            if parsed.kind != "cap":
                raise _fail("target.capability must be a cap: id",
                            "invalid_request", "malformed_target")
            target["capability"] = parsed.canonical
    elif map_type == "work":
        goal = raw.get("goal")
        if goal is None:
            raise _fail("a work map needs target.goal", "invalid_request",
                        "malformed_target")
        goal_text = _clean_text(goal, name="target.goal", limit=128)
        if not goal_text.startswith("goal:"):
            goal_text = f"goal:{goal_text}"
        if not _GOAL_RE.fullmatch(goal_text):
            raise _fail("target.goal is not a goal id", "invalid_request",
                        "malformed_target")
        target["goal"] = goal_text
        if "runs" in raw:
            runs = raw["runs"]
            if not isinstance(runs, (list, tuple)) or isinstance(runs, str):
                raise _fail("target.runs must be a list", "invalid_request",
                            "malformed_target")
            if len(runs) > MAX_RUNS:
                raise _fail(f"target.runs exceeds {MAX_RUNS} entries",
                            "budget_exceeded", "budget_over_hard_bound")
            cleaned = []
            for value in runs:
                run = _clean_text(value, name="target.runs[]", limit=120)
                if not _RUN_RE.fullmatch(run):
                    raise _fail("target.runs entry is malformed",
                                "invalid_request", "malformed_target")
                cleaned.append(run)
            target["runs"] = sorted(set(cleaned))
        if "tests" in raw:
            tests = raw["tests"]
            if not isinstance(tests, (list, tuple)) or isinstance(tests, str):
                raise _fail("target.tests must be a list", "invalid_request",
                            "malformed_target")
            if len(tests) > MAX_TESTS:
                raise _fail(f"target.tests exceeds {MAX_TESTS} entries",
                            "budget_exceeded", "budget_over_hard_bound")
            cleaned_tests = []
            for value in tests:
                test = _clean_text(value, name="target.tests[]", limit=300)
                if not _TEST_RE.fullmatch(test):
                    raise _fail("target.tests entry is malformed",
                                "invalid_request", "malformed_target")
                cleaned_tests.append(test)
            target["tests"] = sorted(set(cleaned_tests))
    return target


def _outline_target_text(value: Any, name: str) -> str:
    text = _clean_text(value, name=name, limit=512)
    if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
        raise _fail(f"{name} must be repository-relative or a graph key",
                    "forbidden", "cross_scope")
    if ".." in text.replace("\\", "/").split("/"):
        raise _fail(f"{name} escapes the repository", "forbidden",
                    "cross_scope")
    return text


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


def encode_cursor(offset: int, digest: str) -> str:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("cursor offset must be an integer")
    if offset < 0 or offset > HARD_MAX_CURSOR_OFFSET:
        raise ValueError("cursor offset is out of bounds")
    if not re.fullmatch(r"[0-9a-f]{12}", digest):
        raise ValueError("cursor digest must be 12 lowercase hex characters")
    return f"v1:{offset}:{digest}"


def decode_cursor(cursor: str) -> tuple[int, str]:
    if not isinstance(cursor, str):
        raise _fail("cursor must be a string", "invalid_request",
                    "malformed_cursor")
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise _fail("cursor is not a model API v1 page token",
                    "invalid_request", "malformed_cursor")
    offset = int(match.group("offset"))
    if offset > HARD_MAX_CURSOR_OFFSET:
        raise _fail("cursor offset is out of bounds", "invalid_request",
                    "malformed_cursor")
    return offset, match.group("digest")


# ---------------------------------------------------------------------------
# Map instances
# ---------------------------------------------------------------------------


def _validate_data(map_type: str, data: Any) -> None:
    if not isinstance(data, Mapping):
        raise _fail("map data must be a mapping", "invalid_request",
                    "schema_mismatch")
    allowed = set(_DATA_REQUIRED) | set(_DATA_OPTIONAL.get(map_type, ()))
    _unknown_fields(data, allowed, what="map.data")
    missing = [name for name in _DATA_REQUIRED if name not in data]
    if missing:
        raise _fail(f"map data is missing fields {missing}", "invalid_request",
                    "schema_mismatch")
    if data["map"] != map_type:
        raise _fail("map data type does not match the schema",
                    "invalid_request", "schema_mismatch")
    if data["state"] not in MAP_STATES:
        raise _fail("map state is not in the v1 vocabulary", "invalid_value",
                    "schema_mismatch")
    try:
        C.freshness(data["freshness"])
    except C.ContractError as exc:
        raise _fail("map freshness is invalid", "invalid_value",
                    "schema_mismatch") from exc
    items = data["items"]
    relations = data["relations"]
    if not isinstance(items, list) or not isinstance(relations, list):
        raise _fail("map items and relations must be lists", "invalid_value",
                    "schema_mismatch")
    if len(items) > HARD_MAX_ITEMS:
        raise _fail("map page exceeds the hard item bound", "budget_exceeded",
                    "budget_over_hard_bound")
    for item in items:
        if not isinstance(item, Mapping):
            raise _fail("map item must be a mapping", "invalid_value",
                        "schema_mismatch")
        for name in ("id", "kind", "freshness", "evidence", "source", "trust"):
            if name not in item:
                raise _fail(f"map item is missing {name}", "invalid_value",
                            "schema_mismatch")
        if item["kind"] not in ITEM_KINDS:
            raise _fail("map item kind is not in the v1 vocabulary",
                        "invalid_value", "schema_mismatch")
        if item["trust"] not in TRUST_LABELS:
            raise _fail("map item trust label is not in the v1 vocabulary",
                        "invalid_value", "schema_mismatch")
        try:
            C.freshness(item["freshness"])
            C.evidence(item["evidence"])
        except C.ContractError as exc:
            raise _fail("map item vocabulary is invalid", "invalid_value",
                        "schema_mismatch") from exc
    for relation in relations:
        if not isinstance(relation, Mapping):
            raise _fail("map relation must be a mapping", "invalid_value",
                        "schema_mismatch")
        for name in ("id", "kind", "type", "from", "to", "freshness",
                     "evidence"):
            if name not in relation:
                raise _fail(f"map relation is missing {name}", "invalid_value",
                            "schema_mismatch")
        if relation["kind"] != "relation":
            raise _fail("map relation kind must be relation", "invalid_value",
                        "schema_mismatch")
        try:
            C.freshness(relation["freshness"])
            C.evidence(relation["evidence"])
        except C.ContractError as exc:
            raise _fail("map relation vocabulary is invalid", "invalid_value",
                        "schema_mismatch") from exc
    if not isinstance(data["counts"], Mapping):
        raise _fail("map counts must be a mapping", "invalid_value",
                    "schema_mismatch")
    for name, value in data["counts"].items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _fail(f"map count {name} must be a non-negative integer",
                        "invalid_value", "schema_mismatch")
    if not isinstance(data["bounds"], Mapping):
        raise _fail("map bounds must be a mapping", "invalid_value",
                    "schema_mismatch")
    if not isinstance(data["source_identities"], Mapping):
        raise _fail("map source_identities must be a mapping", "invalid_value",
                    "schema_mismatch")
    if not isinstance(data["trust"], Mapping):
        raise _fail("map trust must be a mapping", "invalid_value",
                    "schema_mismatch")


def build_instance(map_type: str, *, scope: Mapping[str, Any],
                   snapshot: str, inputs: Iterable[str],
                   provenance: Mapping[str, Any], data: Mapping[str, Any],
                   omissions: Mapping[str, Any] | None = None,
                   cursor: str | None = None, trace_id: str | None = None,
                   created_at: int | str | None = None
                   ) -> tuple[dict[str, Any], str]:
    """Validate a map instance and return ``(normalised_instance, hash)``."""
    if map_type not in MAP_TYPES:
        raise _fail(f"unknown map type {map_type!r}", "unsupported",
                    "unsupported_map")
    provenance_value = dict(provenance)
    provenance_value["schema_fingerprint"] = map_schema_fingerprint(map_type)
    instance: dict[str, Any] = {
        "schema": schema_ref(map_type),
        "scope": dict(scope),
        "snapshot": snapshot,
        "inputs": list(inputs),
        "provenance": provenance_value,
        "data": dict(data),
    }
    if omissions is not None:
        instance["omissions"] = C.validate_truncation(omissions)
    if cursor is not None:
        instance["cursor"] = cursor
    if trace_id is not None:
        instance["trace_id"] = trace_id
    if created_at is not None:
        instance["created_at"] = created_at
    try:
        normalized = C.validate_instance(instance)
    except C.ContractError as exc:
        raise _fail(f"map instance is not canonical: {exc.code}",
                    "invalid_value", "schema_mismatch") from exc
    _validate_data(map_type, normalized["data"])
    try:
        digest = C.instance_hash(normalized)
    except C.ContractError as exc:
        raise _fail(f"map instance hash failed: {exc.code}", "invalid_value",
                    "schema_mismatch") from exc
    return normalized, digest


def validate_map(instance: Mapping[str, Any], *,
                 expected: str | None = None) -> tuple[dict[str, Any], str]:
    """Validate an existing map instance and return ``(normalised, hash)``.

    The schema namespace, required data fields and the provenance schema
    fingerprint must all agree.  An instance from another map type or another
    schema revision is refused rather than reinterpreted.
    """
    try:
        normalized = C.validate_instance(instance)
    except C.ContractError as exc:
        raise _fail(f"map instance is not canonical: {exc.code}",
                    "invalid_value", "schema_mismatch") from exc
    namespace = normalized["schema"]["namespace"]
    if not namespace.startswith(MAP_NAMESPACE_PREFIX):
        raise _fail("instance is not a model API map", "invalid_request",
                    "schema_mismatch")
    map_type = namespace[len(MAP_NAMESPACE_PREFIX):]
    if map_type not in MAP_TYPES:
        raise _fail("instance has an unknown map namespace", "unsupported",
                    "unsupported_map")
    if expected is not None and map_type != expected:
        raise _fail(f"instance is a {map_type} map, expected {expected}",
                    "invalid_request", "schema_mismatch")
    provenance = normalized.get("provenance") or {}
    fingerprint = provenance.get("schema_fingerprint")
    if fingerprint != map_schema_fingerprint(map_type):
        raise _fail("instance schema fingerprint does not match the v1 schema",
                    "invalid_request", "schema_mismatch")
    _validate_data(map_type, normalized["data"])
    try:
        digest = C.instance_hash(normalized)
    except C.ContractError as exc:
        raise _fail(f"map instance hash failed: {exc.code}", "invalid_value",
                    "schema_mismatch") from exc
    return normalized, digest


# ---------------------------------------------------------------------------
# Fact projections, plain rendering and representation views
# ---------------------------------------------------------------------------


def _item_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name in ("type", "key", "result", "state", "summary", "text"):
        if name in item and item[name] not in (None, "", []):
            value[name] = item[name]
    return {
        "subject": str(item["id"]),
        "kind": str(item["kind"]),
        "value": value,
        "freshness": str(item["freshness"]),
        "evidence": str(item["evidence"]),
    }


def _relation_fact(relation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "subject": str(relation["id"]),
        "kind": "relation",
        "value": {
            "type": str(relation["type"]),
            "from": str(relation["from"]),
            "to": str(relation["to"]),
        },
        "freshness": str(relation["freshness"]),
        "evidence": str(relation["evidence"]),
    }


def map_facts(instance: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The canonical fact list shared by structured and plain output.

    Supported facts are the map state, each source identity, each item and
    each relation.  Nothing here is inferred from a different representation:
    the plain renderer consumes exactly this list.
    """
    data = instance["data"]
    facts: list[dict[str, Any]] = [{
        "subject": f"map:{data['map']}",
        "kind": "map",
        "value": {"state": data["state"], "freshness": data["freshness"]},
        "freshness": data["freshness"],
        "evidence": ("observed" if data["state"] in ("ok", "degraded")
                     else "inferred"),
    }]
    identities = data.get("source_identities") or {}
    source_rows: list[tuple[str, Mapping[str, Any]]] = []
    for name in sorted(identities):
        value = identities[name]
        if name == "sources" and isinstance(value, list):
            for index, row in enumerate(value):
                if isinstance(row, Mapping):
                    source_rows.append((f"sources[{index}]", row))
        elif isinstance(value, Mapping):
            source_rows.append((name, value))
    for name, row in source_rows:
        state = str(row.get("state") or "unknown")
        freshness = row.get("freshness")
        if freshness not in C.FRESHNESS:
            freshness = ("current" if state == "observed" else
                         "absent" if state == "absent" else "unknown")
        facts.append({
            "subject": f"source:{name}",
            "kind": "source",
            "value": {
                "id": row.get("id"),
                "state": state,
                "hash": row.get("hash"),
            },
            "freshness": freshness,
            "evidence": ("observed" if state == "observed" else "declared"),
        })
    for item in data.get("items") or []:
        facts.append(_item_fact(item))
    for relation in data.get("relations") or []:
        facts.append(_relation_fact(relation))
    return facts


def _scope_line(scope: Mapping[str, Any]) -> str:
    parts = []
    for name in ("session", "model", "repo", "checkout", "snapshot"):
        value = scope.get(name)
        if value:
            parts.append(f"{name}={value}")
    root = scope.get("root")
    if root:
        parts.append(f"root={root}")
    paths = scope.get("paths")
    if paths:
        parts.append("paths=" + ",".join(str(path) for path in paths))
    return " ".join(parts) or "scope=unspecified"


def render_plain(instance: Mapping[str, Any]) -> str:
    """Render an L0-style plain view from the shared fact list.

    The rendering is deterministic and bounded by the map page that produced
    it.  It contains no information absent from the structured instance and
    no full source bodies.
    """
    data = instance["data"]
    provenance = instance.get("provenance") or {}
    omissions = instance.get("omissions")
    lines = [
        "ozagia model map (plain v1)",
        f"map: {data['map']}",
        f"state: {data['state']}  freshness: {data['freshness']}",
        f"schema: {instance['schema']['namespace']}@{instance['schema']['version']}",
        f"schema_fingerprint: {provenance.get('schema_fingerprint')}",
        f"snapshot: {instance['snapshot']}",
        f"scope: {_scope_line(instance.get('scope') or {})}",
        "trust: content=data authority=none",
        "sources:",
    ]
    identities = data.get("source_identities") or {}
    for name, value in sorted(identities.items()):
        if name == "sources" and isinstance(value, list):
            for index, row in enumerate(value):
                lines.append(f"- sources[{index}] {C.canonical_json(row)}")
        elif isinstance(value, Mapping):
            lines.append(f"- {name} {C.canonical_json(value)}")
    lines.append("facts:")
    for fact in map_facts(instance):
        if fact["kind"] == "source":
            continue  # sources are already listed above
        value = C.canonical_json(fact["value"])
        lines.append(
            f"- {fact['kind']} {fact['subject']} "
            f"[{fact['freshness']}/{fact['evidence']}] {value}")
    if data.get("relations"):
        lines.append("relations:")
        for relation in data["relations"]:
            lines.append(
                f"- {relation['type']} {relation['from']} -> "
                f"{relation['to']} [{relation['freshness']}/"
                f"{relation['evidence']}]")
    if data.get("not_assessed"):
        lines.append("not_assessed:")
        lines.extend(f"- {item}" for item in data["not_assessed"])
    if data.get("deferred"):
        lines.append("deferred:")
        lines.extend(f"- {item}" for item in data["deferred"])
    if data.get("notices"):
        lines.append("notices:")
        lines.extend(f"- {item}" for item in data["notices"])
    lines.append("omissions: " + C.canonical_json(
        omissions if omissions is not None else C.truncation().to_dict()))
    return "\n".join(lines) + "\n"


def plain_view(instance: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "plain",
        "version": MAP_VERSION,
        "text": render_plain(instance),
        "facts": map_facts(instance),
        "omissions": (instance.get("omissions")
                      or C.truncation().to_dict()),
    }


def structured_view(instance: Mapping[str, Any],
                    digest: str | None = None) -> dict[str, Any]:
    if digest is None:
        _, digest = validate_map(instance)
    return {
        "kind": "structured",
        "version": MAP_VERSION,
        "map_hash": digest,
        "instance": dict(instance),
        "facts": map_facts(instance),
        "omissions": (instance.get("omissions")
                      or C.truncation().to_dict()),
    }


def representation(map_type: str, instance: Mapping[str, Any], *,
                   format: str, digest: str | None = None) -> dict[str, Any]:
    if format == "plain":
        return plain_view(instance)
    if format != "structured":
        raise _fail(f"unknown representation {format!r}", "unsupported",
                    "unsupported_format")
    return structured_view(instance, digest)


__all__ = [
    "DEFAULT_BUDGETS", "DEFAULT_PAGE_ITEMS", "FORMATS", "HARD_BUDGETS",
    "HARD_MAX_BYTES", "HARD_MAX_DEPTH", "HARD_MAX_ITEMS",
    "HARD_MAX_CURSOR_OFFSET", "ITEM_KINDS", "MAP_LABELS", "MAP_NAMESPACE_PREFIX",
    "MAP_STATES", "MAP_TYPES", "MAP_VERSION", "MAX_TARGETS", "MAX_RUNS",
    "MAX_TESTS", "MAX_TRACE_ID", "REQUEST_NAMESPACE", "REQUEST_VERSION",
    "RESPONSE_NAMESPACE", "TRUST_LABELS", "build_instance", "decode_cursor",
    "encode_cursor", "make_request", "map_facts", "map_schema",
    "map_schema_fingerprint", "plain_view", "render_plain", "representation",
    "schema_ref", "structured_view", "validate_map", "validate_request",
]
