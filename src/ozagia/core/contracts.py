"""Version-1 self-graph contracts: canonical JSON, identities, facts and errors.

This module is the single authority for the shapes and hashes that the
self-graph work packages share.  Everything here is pure and stdlib-only: no
file, environment, clock, network or process access happens in these
functions.  Callers inject observations (``observed_at``, hashes, cursors) and
own all I/O.

Canonical JSON
--------------

``canonical_json`` is Ozagia's canonical form, **not** RFC 8785 (JCS).  The
value model is deliberately narrow:

* ``dict`` with ``str`` keys, ``list``, ``str``, ``bool``, ``None``, and
  ``int`` in the signed 64-bit range;
* floats (including NaN and infinities), bytes, tuples, sets and any other
  type are rejected with :class:`CanonicalJSONError`;
* keys are sorted by Unicode code point, output uses compact separators,
  UTF-8 with ``ensure_ascii=False`` and no trailing newline;
* strings with lone surrogates are rejected (they have no UTF-8 form);
* two distinct keys in one mapping that collapse to the same Unicode NFC form
  are rejected as a collision.  Keys are otherwise hashed exactly as supplied.

Schema and instance records
---------------------------

A schema record has exactly ``namespace``, ``version``, ``types``,
``required``, ``constraints`` and ``sensitivity``.  ``x-`` extension fields
are allowed.  ``schema_fingerprint`` hashes a normalized subset of those six
fields only (extensions excluded) as canonical JSON.  ``required`` is sorted
and ``version`` is normalized to ``major.minor`` before hashing.

An instance record has ``schema``, ``scope``, ``snapshot``, ``inputs``,
``provenance``, ``data`` and optionally ``omissions``, ``created_at``,
``cursor`` and ``trace_id``; ``x-`` extensions are allowed.  ``instance_hash``
hashes the canonical instance *excluding* ``created_at``, ``cursor`` and
``trace_id`` and any ``x-`` field the caller declares transport-only.  Size
and content hash are computed beside the instance, never inside it, so the
hash cannot include itself.

Version policy
--------------

Only major version 1 is accepted in this release.  Versions are spelled
``major.minor`` (``"1"`` is accepted and normalized to ``"1.0"``); a
different major raises :class:`UnsupportedVersionError`.  Schemas and
instances reject unknown non-``x-`` fields with :class:`UnknownFieldError`.
Extensions are included in instance hashes unless declared transport-only.
The deprecation window is one minor release, recorded in
``docs/self_graph_contracts.md``.

Identities
----------

IDs are ``<kind>:<body>`` with kinds ``cap``, ``impl``, ``iface``, ``run``,
``goal``, ``repo``, ``checkout``, ``snapshot`` and ``project``.  Components
are lowercase, bounded and ASCII; a literal ``/``, ``:``, ``@`` or ``%`` in a
component is escaped as ``%2F``, ``%3A``, ``%40`` or ``%25`` and escapes are
canonical uppercase hex only.  Existing unqualified graph keys are never
rewritten: :func:`qualify` only prefixes a key at a projection boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Vocabulary and limits
# ---------------------------------------------------------------------------

SUPPORTED_MAJOR = 1
CURRENT_VERSION = "1.0"
FACT_VERSION = "1.0"
RELATION_VERSION = "1.0"

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

TRANSPORT_FIELDS = frozenset({"created_at", "cursor", "trace_id"})
EXTENSION_PREFIX = "x-"

FRESHNESS = ("current", "stale", "unknown", "absent")
EVIDENCE = ("declared", "inferred", "observed", "verified")
RESULTS = (
    "declared", "pending", "passed", "failed", "unknown", "invalid",
    "not_assessed",
)
READINESS_DIMENSIONS = (
    "installed", "configured", "reachable", "trusted", "permitted",
)
READINESS_UNKNOWN = "unknown"
CLOCKS = ("system", "monotonic", "injected", "unknown")
SENSITIVITIES = ("public", "internal", "private", "secret")
# Default map budgets from the M0 decision table.  WP5 enforces them; the
# caller-raisable hard bound is still an open decision (see the docs).
MAP_DEFAULT_MAX_BYTES = 64 * 1024
MAP_DEFAULT_MAX_NODES = 200
MAP_DEFAULT_MAX_DEPTH = 2
TRUNCATION_REASONS = frozenset({
    "limit", "bytes", "depth", "budget", "scope", "unavailable", "filtered",
})

CONTRACT_ERROR_CODES = frozenset({
    "contract.invalid_value",
    "contract.float_not_allowed",
    "contract.int_out_of_range",
    "contract.invalid_key",
    "contract.duplicate_key",
    "contract.invalid_unicode",
    "contract.invalid_id",
    "contract.id_collision",
    "contract.invalid_version",
    "contract.unsupported_version",
    "contract.unknown_field",
    "contract.invalid_vocabulary",
    "contract.bounds_exceeded",
    "contract.invalid_schema",
    "contract.invalid_service_error",
    "contract.invalid_truncation",
})

SERVICE_ERROR_CODES = frozenset({
    "invalid_request", "invalid_value", "unknown_field",
    "unsupported_version", "not_found", "unavailable", "unauthorized",
    "forbidden", "conflict", "stale", "unknown", "timeout", "cancelled",
    "budget_exceeded", "truncated", "unsupported", "invalid_state",
    "precondition_failed", "internal",
})

ERROR_CODES = CONTRACT_ERROR_CODES | SERVICE_ERROR_CODES

MAX_CANON_DEPTH = 64
MAX_CANON_NODES = 100_000
MAX_FACT_BYTES = 8 * 1024
MAX_VALUE_DEPTH = 8
MAX_VALUE_NODES = 512
MAX_SCOPE_ITEMS = 32
MAX_TEXT = 512
MAX_ERROR_MESSAGE = 512
MAX_DETAILS_BYTES = 4 * 1024
MAX_CURSOR = 256

MAX_ID_LENGTH = 512
MAX_COMPONENT_LENGTH = 128
MAX_LOCAL_KEY_LENGTH = 256


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """A contract violation with a stable, machine-readable ``code``."""

    code = "contract.invalid_value"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        if resolved not in ERROR_CODES:
            raise ValueError(f"unknown contract error code {resolved!r}")
        self.code = resolved
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class CanonicalJSONError(ContractError):
    code = "contract.invalid_value"


class FloatNotAllowedError(CanonicalJSONError):
    code = "contract.float_not_allowed"


class IntOutOfRangeError(CanonicalJSONError):
    code = "contract.int_out_of_range"


class InvalidKeyError(CanonicalJSONError):
    code = "contract.invalid_key"


class DuplicateKeyError(CanonicalJSONError):
    code = "contract.duplicate_key"


class InvalidUnicodeError(CanonicalJSONError):
    code = "contract.invalid_unicode"


class IDError(ContractError):
    code = "contract.invalid_id"


class IDCollisionError(IDError):
    code = "contract.id_collision"


class VersionError(ContractError):
    code = "contract.invalid_version"


class UnsupportedVersionError(VersionError):
    code = "contract.unsupported_version"


class UnknownFieldError(ContractError):
    code = "contract.unknown_field"


class VocabularyError(ContractError):
    code = "contract.invalid_vocabulary"


class BoundsError(ContractError):
    code = "contract.bounds_exceeded"


class SchemaError(ContractError):
    code = "contract.invalid_schema"


class TruncationError(ContractError):
    code = "contract.invalid_truncation"


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------

_RAW_COMPONENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789._-")
_ESCAPES = {"2F": "/", "3A": ":", "40": "@", "25": "%"}
_ESCAPE_FOR = {"/": "2F", ":": "3A", "@": "40", "%": "25"}


def _check_unicode(text: str, path: str) -> None:
    for char in text:
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise InvalidUnicodeError(
                f"{path}: lone surrogate U+{ord(char):04X} has no UTF-8 form")


def _validate_canonical(value: Any, *, path: str = "$", depth: int = 0,
                        nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_CANON_NODES:
        raise BoundsError(
            f"{path}: canonical value exceeds {MAX_CANON_NODES} nodes")
    if depth > MAX_CANON_DEPTH:
        raise BoundsError(
            f"{path}: canonical value exceeds depth {MAX_CANON_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _check_unicode(value, path)
        return
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise IntOutOfRangeError(
                f"{path}: integer {value} is outside signed 64-bit range")
        return
    if isinstance(value, float):
        raise FloatNotAllowedError(
            f"{path}: floats (including NaN and infinities) are not canonical")
    if isinstance(value, dict):
        normalized: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidKeyError(
                    f"{path}: mapping keys must be strings, got "
                    f"{type(key).__name__}")
            _check_unicode(key, f"{path}.<key>")
            folded = unicodedata.normalize("NFC", key)
            previous = normalized.get(folded)
            if previous is not None and previous != key:
                raise DuplicateKeyError(
                    f"{path}: keys {previous!r} and {key!r} collide after NFC "
                    "normalization")
            normalized[folded] = key
            _validate_canonical(item, path=f"{path}.{key}", depth=depth + 1,
                                nodes=nodes)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical(item, path=f"{path}[{index}]",
                                depth=depth + 1, nodes=nodes)
        return
    raise CanonicalJSONError(
        f"{path}: {type(value).__name__} is not a canonical JSON value")


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text for ``value`` (see module docstring)."""
    _validate_canonical(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    """SHA-256 hex digest of ``canonical_json(value)`` encoded as UTF-8."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_value(value: Any, *, path: str, max_bytes: int,
                   max_depth: int = MAX_VALUE_DEPTH,
                   max_nodes: int = MAX_VALUE_NODES) -> str:
    """Validate a canonical value and return its canonical text, bounded."""
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise BoundsError(f"{path}: value exceeds {max_bytes} canonical bytes")

    def walk(item: Any, depth: int, counter: list[int]) -> None:
        counter[0] += 1
        if counter[0] > max_nodes:
            raise BoundsError(f"{path}: value exceeds {max_nodes} nodes")
        if depth > max_depth:
            raise BoundsError(f"{path}: value exceeds depth {max_depth}")
        if isinstance(item, dict):
            for child in item.values():
                walk(child, depth + 1, counter)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1, counter)

    walk(value, 0, [0])
    return encoded


# ---------------------------------------------------------------------------
# Versions and schemas
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d*)"
                         r"(?:\.(?P<minor>0|[1-9]\d*))?$")
_NAMESPACE_RE = re.compile(
    r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


def parse_version(text: str) -> tuple[int, int]:
    """Parse ``major[.minor]`` for the supported major version."""
    if not isinstance(text, str):
        raise VersionError(f"version must be a string, got {type(text).__name__}")
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        raise VersionError(f"invalid version {text!r}; expected major[.minor]")
    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    if major != SUPPORTED_MAJOR:
        raise UnsupportedVersionError(
            f"unsupported major version {major}; only {SUPPORTED_MAJOR} is "
            "accepted in this release")
    return major, minor


def normalize_version(text: str) -> str:
    """Return the canonical ``major.minor`` spelling (``"1"`` -> ``"1.0"``)."""
    major, minor = parse_version(text)
    return f"{major}.{minor}"


def _check_namespace(namespace: Any) -> str:
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise SchemaError(f"invalid schema namespace {namespace!r}")
    if len(namespace) > 128:
        raise SchemaError("schema namespace is longer than 128 characters")
    return namespace


def _check_extension_fields(record: Mapping[str, Any], allowed: Iterable[str],
                            *, what: str) -> None:
    allowed_set = set(allowed)
    for key in record:
        if not isinstance(key, str):
            raise UnknownFieldError(
                f"{what}: field names must be strings, got "
                f"{type(key).__name__}")
        if key in allowed_set or key.startswith(EXTENSION_PREFIX):
            continue
        raise UnknownFieldError(f"{what}: unknown field {key!r}")


SCHEMA_FIELDS = (
    "namespace", "version", "types", "required", "constraints", "sensitivity",
)


def validate_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a schema record and return a normalized copy.

    Extensions are preserved but excluded from :func:`schema_fingerprint`.
    """
    if not isinstance(schema, Mapping):
        raise SchemaError("schema must be a mapping")
    _check_extension_fields(schema, SCHEMA_FIELDS, what="schema")
    missing = [name for name in SCHEMA_FIELDS if name not in schema]
    if missing:
        raise SchemaError(f"schema is missing required fields: {missing}")
    namespace = _check_namespace(schema["namespace"])
    version = normalize_version(schema["version"])
    types = schema["types"]
    if not isinstance(types, Mapping):
        raise SchemaError("schema.types must be a mapping")
    normalized_types: dict[str, Any] = {}
    for name, definition in types.items():
        if not isinstance(name, str) or not _TYPE_NAME_RE.fullmatch(name):
            raise SchemaError(f"invalid type name {name!r}")
        if not isinstance(definition, Mapping):
            raise SchemaError(f"type {name!r} definition must be a mapping")
        normalized_types[name] = dict(definition)
    required = schema["required"]
    if not isinstance(required, list):
        raise SchemaError("schema.required must be a list of field names")
    names: set[str] = set()
    for name in required:
        if not isinstance(name, str) or not _FIELD_NAME_RE.fullmatch(name):
            raise SchemaError(f"invalid required field name {name!r}")
        if name in names:
            raise SchemaError(f"duplicate required field {name!r}")
        names.add(name)
    constraints = schema["constraints"]
    if not isinstance(constraints, Mapping):
        raise SchemaError("schema.constraints must be a mapping")
    sensitivity = schema["sensitivity"]
    if sensitivity not in SENSITIVITIES:
        raise VocabularyError(
            f"invalid sensitivity {sensitivity!r}; expected one of "
            f"{SENSITIVITIES}")
    normalized: dict[str, Any] = {
        "namespace": namespace,
        "version": version,
        "types": normalized_types,
        "required": sorted(required),
        "constraints": dict(constraints),
        "sensitivity": sensitivity,
    }
    for key, value in schema.items():
        if key.startswith(EXTENSION_PREFIX):
            normalized[key] = value
    canonical_json(normalized)
    return normalized


def schema_fingerprint(schema: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical normalized schema subset.

    The subset is exactly ``namespace``, ``version``, ``types``, ``required``
    (sorted), ``constraints`` and ``sensitivity``.  ``x-`` extensions do not
    affect the fingerprint.
    """
    normalized = validate_schema(schema)
    subset = {name: normalized[name] for name in SCHEMA_FIELDS}
    return content_hash(subset)


INSTANCE_FIELDS = (
    "schema", "scope", "snapshot", "inputs", "provenance", "omissions",
    "created_at", "cursor", "trace_id", "data",
)


def _validate_schema_ref(ref: Any) -> dict[str, Any]:
    if not isinstance(ref, Mapping):
        raise SchemaError("instance.schema must be a mapping")
    _check_extension_fields(ref, ("namespace", "version"),
                            what="instance.schema")
    namespace = _check_namespace(ref.get("namespace"))
    version = normalize_version(ref.get("version"))
    normalized: dict[str, Any] = {"namespace": namespace, "version": version}
    for key, value in ref.items():
        if key.startswith(EXTENSION_PREFIX):
            normalized[key] = value
    return normalized


def _validate_snapshot(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError("instance.snapshot must be a non-empty string")
    if len(value) > MAX_TEXT or any(ord(c) < 0x20 for c in value):
        raise SchemaError("instance.snapshot is malformed or too long")
    return value


def _validate_inputs(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise SchemaError("instance.inputs must be a list of strings")
    if len(value) > 256:
        raise BoundsError("instance.inputs exceeds 256 entries")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_TEXT:
            raise SchemaError("instance.inputs entries must be bounded strings")
        result.append(item)
    return result


def _validate_timestamp(value: Any) -> int | str:
    if isinstance(value, bool):
        raise SchemaError("created_at must be an integer or string, not bool")
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise IntOutOfRangeError("created_at is outside signed 64-bit range")
        return value
    if isinstance(value, str):
        if not value or len(value) > 64 or any(ord(c) < 0x20 for c in value):
            raise SchemaError("created_at string is malformed or too long")
        return value
    raise SchemaError("created_at must be an integer or string")


def _validate_optional_text(value: Any, name: str,
                            limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SchemaError(f"{name} must be a bounded string or null")
    if any(ord(c) < 0x20 for c in value):
        raise SchemaError(f"{name} contains control characters")
    return value


def validate_instance(instance: Mapping[str, Any], *,
                      transport_only: Iterable[str] = ()) -> dict[str, Any]:
    """Validate an instance record and return a normalized copy.

    ``transport_only`` may name top-level ``x-`` fields to exclude from
    :func:`instance_hash`.  Naming any other field is an error, so a caller
    cannot accidentally hide data from the hash.
    """
    if not isinstance(instance, Mapping):
        raise SchemaError("instance must be a mapping")
    _check_extension_fields(instance, INSTANCE_FIELDS, what="instance")
    declared = tuple(transport_only)
    for name in declared:
        if not isinstance(name, str):
            raise SchemaError("transport_only entries must be strings")
        if name not in TRANSPORT_FIELDS and not name.startswith(EXTENSION_PREFIX):
            raise SchemaError(
                f"transport_only field {name!r} is not a transport or x- field")
    missing = [name for name in ("schema", "scope", "snapshot", "inputs",
                                 "provenance", "data") if name not in instance]
    if missing:
        raise SchemaError(f"instance is missing required fields: {missing}")
    scope = instance["scope"]
    provenance = instance["provenance"]
    if not isinstance(scope, Mapping):
        raise SchemaError("instance.scope must be a mapping")
    if not isinstance(provenance, Mapping):
        raise SchemaError("instance.provenance must be a mapping")
    omissions = instance.get("omissions")
    if omissions is not None:
        omissions = validate_truncation(omissions)
    normalized: dict[str, Any] = {
        "schema": _validate_schema_ref(instance["schema"]),
        "scope": dict(scope),
        "snapshot": _validate_snapshot(instance["snapshot"]),
        "inputs": _validate_inputs(instance["inputs"]),
        "provenance": dict(provenance),
        "data": instance["data"],
    }
    if "omissions" in instance:
        normalized["omissions"] = omissions
    if "created_at" in instance:
        normalized["created_at"] = _validate_timestamp(instance["created_at"])
    if "cursor" in instance:
        normalized["cursor"] = _validate_optional_text(
            instance["cursor"], "cursor", MAX_CURSOR)
    if "trace_id" in instance:
        normalized["trace_id"] = _validate_optional_text(
            instance["trace_id"], "trace_id")
    for key, value in instance.items():
        if key.startswith(EXTENSION_PREFIX):
            normalized[key] = value
    canonical_json(normalized)
    return normalized


def instance_hash(instance: Mapping[str, Any], *,
                  transport_only: Iterable[str] = ()) -> str:
    """SHA-256 of the canonical instance without transport-only fields.

    Always excluded: ``created_at``, ``cursor`` and ``trace_id``.  Additional
    ``x-`` fields may be declared transport-only by the caller.
    """
    normalized = validate_instance(instance, transport_only=transport_only)
    skip = set(TRANSPORT_FIELDS) | set(transport_only)
    subset = {key: value for key, value in normalized.items()
              if key not in skip}
    return content_hash(subset)


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

ID_KINDS = (
    "cap", "impl", "iface", "run", "goal", "repo", "checkout", "snapshot",
    "project",
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DECODED_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@%\-]*$")
_HASH12_RE = re.compile(r"^[0-9a-f]{12}$")
_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_PROJECT_RE = re.compile(
    r"^project:(?P<repo>[^@]+)@(?P<checkout>[0-9a-f]{12}):"
    r"(?P<snapshot>[0-9a-f]{12,64})/(?P<key>.+)$")


@dataclass(frozen=True)
class Identity:
    """A parsed, canonical self-graph identity."""

    kind: str
    canonical: str
    components: tuple[str, ...]

    @property
    def prefix(self) -> str:
        return f"{self.kind}:"


def _encode_component(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise IDError("identity component must be a non-empty string")
    if len(raw) > MAX_COMPONENT_LENGTH:
        raise IDError(
            f"identity component exceeds {MAX_COMPONENT_LENGTH} characters")
    out: list[str] = []
    for char in raw:
        if char in _ESCAPE_FOR:
            out.append("%" + _ESCAPE_FOR[char])
        elif char in _RAW_COMPONENT_CHARS:
            out.append(char)
        else:
            raise IDError(
                f"identity component {raw!r} contains disallowed character "
                f"{char!r}; IDs are lowercase ASCII")
    encoded = "".join(out)
    if not _DECODED_COMPONENT_RE.fullmatch(raw):
        raise IDError(
            f"identity component {raw!r} must start with a lowercase letter "
            "or digit and contain only [a-z0-9._-] plus escaped / : @ %")
    return encoded


def _decode_component(text: str, *, what: str = "component") -> str:
    if not isinstance(text, str) or not text:
        raise IDError(f"{what} is empty")
    if len(text) > MAX_COMPONENT_LENGTH * 3:
        raise IDError(f"{what} is too long")
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "%":
            pair = text[index + 1:index + 3]
            if len(pair) != 2 or any(c not in "0123456789ABCDEF" for c in pair):
                raise IDError(
                    f"{what}: escapes must be uppercase %XX (got "
                    f"{text[index:index + 3]!r})")
            escaped = _ESCAPES.get(pair)
            if escaped is None:
                raise IDError(f"{what}: escape %{pair} is not allowed; only "
                              "%2F, %3A, %40 and %25 are defined")
            out.append(escaped)
            index += 3
            continue
        if char not in _RAW_COMPONENT_CHARS:
            raise IDError(
                f"{what}: {char!r} is not allowed; use lowercase ASCII or an "
                "escape")
        out.append(char)
        index += 1
    decoded = "".join(out)
    if not _DECODED_COMPONENT_RE.fullmatch(decoded):
        raise IDError(
            f"{what}: decoded value {decoded!r} is not a valid component")
    if _encode_component(decoded) != text:
        raise IDError(f"{what}: non-canonical escape spelling in {text!r}")
    return decoded


def _check_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise IDError(
            f"{what} {name!r} must be lowercase dotted segments matching "
            "[a-z][a-z0-9_-]*")
    if len(name) > MAX_COMPONENT_LENGTH:
        raise IDError(f"{what} is longer than {MAX_COMPONENT_LENGTH} characters")
    return name


def _repo_from_body(body: str) -> tuple[Identity, str, str]:
    parts = body.split("/")
    if len(parts) != 2:
        raise IDError("repo ID body must be <owner>/<name>")
    owner = _decode_component(parts[0], what="repo owner")
    name = _decode_component(parts[1], what="repo name")
    if "/" in name:
        raise IDError("repo name must not contain an escaped slash")
    if not _COMPONENT_RE.fullmatch(name):
        raise IDError(f"invalid repo name {name!r}")
    canonical = f"repo:{_encode_component(owner)}/{_encode_component(name)}"
    if canonical != f"repo:{body}":
        raise IDError(f"repo ID is not canonical: {body!r}")
    return Identity("repo", canonical, (owner, name)), owner, name


def _checkout_from_body(body: str) -> tuple[Identity, str]:
    parts = body.split("@")
    if len(parts) != 2:
        raise IDError("checkout ID body must be <repo>@<12 hex>")
    repo = _repo_from_body(parts[0])[0]
    fingerprint = parts[1]
    if not _HASH12_RE.fullmatch(fingerprint):
        raise IDError(
            "checkout fingerprint must be exactly 12 lowercase hex characters")
    canonical = f"checkout:{parts[0]}@{fingerprint}"
    return Identity("checkout", canonical,
                    (*repo.components, fingerprint)), parts[0]


def _snapshot_from_body(body: str) -> tuple[Identity, str]:
    if "@" not in body:
        raise IDError("snapshot ID body must be <checkout>@<graph hash>")
    checkout_body, graph_hash = body.rsplit("@", 1)
    checkout = _checkout_from_body(checkout_body)[0]
    if not _HASH_RE.fullmatch(graph_hash):
        raise IDError(
            "snapshot graph hash must be 12 to 64 lowercase hex characters")
    canonical = f"snapshot:{checkout_body}@{graph_hash}"
    return Identity("snapshot", canonical,
                    (*checkout.components, graph_hash)), checkout_body


def _check_local_key(key: Any) -> str:
    if not isinstance(key, str) or not key:
        raise IDError("local graph key must be a non-empty string")
    if len(key) > MAX_LOCAL_KEY_LENGTH:
        raise IDError(
            f"local graph key exceeds {MAX_LOCAL_KEY_LENGTH} characters")
    if any(ord(char) < 0x20 for char in key):
        raise IDError("local graph key contains control characters")
    return key


def repo_id(owner: str, name: str) -> str:
    """Build ``repo:<owner>/<name>`` from decoded components."""
    if not isinstance(name, str) or "/" in name:
        raise IDError("repo name must be one component without a slash")
    return (f"repo:{_encode_component(owner)}/"
            f"{_encode_component(name)}")


def checkout_id(repo: str, fingerprint: str) -> str:
    """Build ``checkout:<repo>@<sha256(realpath)[:12]>``.

    ``fingerprint`` may be a full SHA-256 hex digest or an already truncated
    one; the first 12 lowercase hex characters are used.
    """
    parsed = parse_id(repo)
    if parsed.kind != "repo":
        raise IDError(f"checkout base must be a repo ID, got {parsed.kind}")
    if not isinstance(fingerprint, str) or not _HASH_RE.fullmatch(fingerprint):
        raise IDError(
            "checkout fingerprint must be 12 to 64 lowercase hex characters")
    return f"checkout:{parsed.canonical[5:]}@{fingerprint[:12]}"


def snapshot_id(checkout: str, graph_hash: str) -> str:
    """Build ``snapshot:<checkout>@<graph meta hash>`` (12..64 hex)."""
    parsed = parse_id(checkout)
    if parsed.kind != "checkout":
        raise IDError(f"snapshot base must be a checkout ID, got {parsed.kind}")
    if not isinstance(graph_hash, str) or not _HASH_RE.fullmatch(graph_hash):
        raise IDError("snapshot graph hash must be 12 to 64 lowercase hex")
    return f"snapshot:{parsed.canonical[9:]}@{graph_hash}"


def qualify(repo: str, checkout: str, snapshot: str, key: str) -> str:
    """Project a local graph key as ``project:<repo>@<checkout>:<snap>/<key>``.

    The key is copied verbatim.  This is a projection helper only; stored
    graph keys are never rewritten.
    """
    repo_ident = parse_id(repo)
    checkout_ident = parse_id(checkout)
    snapshot_ident = parse_id(snapshot)
    if repo_ident.kind != "repo":
        raise IDError(f"qualify expects a repo ID, got {repo_ident.kind}")
    if checkout_ident.kind != "checkout":
        raise IDError(f"qualify expects a checkout ID, got {checkout_ident.kind}")
    if snapshot_ident.kind != "snapshot":
        raise IDError(f"qualify expects a snapshot ID, got {snapshot_ident.kind}")
    if checkout_ident.components[:2] != repo_ident.components:
        raise IDError("checkout does not belong to the supplied repo")
    if snapshot_ident.components[:3] != checkout_ident.components:
        raise IDError("snapshot does not belong to the supplied checkout")
    local = _check_local_key(key)
    repo_body = repo_ident.canonical[5:]
    checkout_hash = checkout_ident.components[2]
    snapshot_hash = snapshot_ident.components[3]
    result = (f"project:{repo_body}@{checkout_hash}:{snapshot_hash}/{local}")
    if len(result) > MAX_ID_LENGTH:
        raise IDError(f"project ID exceeds {MAX_ID_LENGTH} characters")
    return result


def local_key(project: str) -> str:
    """Return the verbatim local key from a ``project:`` ID."""
    parsed = parse_id(project)
    if parsed.kind != "project":
        raise IDError(f"expected a project ID, got {parsed.kind}")
    return parsed.components[-1]


def parse_id(text: str) -> Identity:
    """Parse and validate one self-graph identity."""
    if not isinstance(text, str) or not text:
        raise IDError("identity must be a non-empty string")
    if len(text) > MAX_ID_LENGTH:
        raise IDError(f"identity exceeds {MAX_ID_LENGTH} characters")
    if any(ord(char) > 0x7F for char in text):
        raise IDError("identities are lowercase ASCII")
    prefix, sep, body = text.partition(":")
    if not sep or prefix not in ID_KINDS:
        raise IDError(
            f"unknown identity kind {prefix!r}; expected one of {ID_KINDS}")
    if not body:
        raise IDError("identity body is empty")
    if any(ord(char) < 0x20 for char in body):
        raise IDError("identity contains control characters")

    if prefix in ("cap", "impl", "iface"):
        return Identity(prefix, f"{prefix}:{_check_name(body, prefix)}",
                        (_check_name(body, prefix),))
    if prefix in ("run", "goal"):
        component = _decode_component(body, what=f"{prefix} ID")
        return Identity(prefix, f"{prefix}:{_encode_component(component)}",
                        (component,))
    if prefix == "repo":
        return _repo_from_body(body)[0]
    if prefix == "checkout":
        return _checkout_from_body(body)[0]
    if prefix == "snapshot":
        return _snapshot_from_body(body)[0]
    if prefix == "project":
        match = _PROJECT_RE.fullmatch(text)
        if match is None:
            raise IDError(
                "project ID must be "
                "project:<repo>@<12 hex>:<12..64 hex>/<local key>")
        repo_body = match.group("repo")
        repo_ident = _repo_from_body(repo_body)[0]
        checkout_hash = match.group("checkout")
        snapshot_hash = match.group("snapshot")
        local = _check_local_key(match.group("key"))
        canonical = (f"project:{repo_body}@{checkout_hash}:{snapshot_hash}/"
                     f"{local}")
        if len(canonical) > MAX_ID_LENGTH:
            raise IDError(f"project ID exceeds {MAX_ID_LENGTH} characters")
        return Identity(
            "project", canonical,
            (*repo_ident.components, checkout_hash, snapshot_hash, local))
    raise IDError(f"unknown identity kind {prefix!r}")


def canonical_id(text: str) -> str:
    """Return the canonical spelling of ``text`` or raise :class:`IDError`."""
    return parse_id(text).canonical


def format_id(kind: str, *parts: str) -> str:
    """Build an identity from a kind and decoded components."""
    if kind not in ID_KINDS:
        raise IDError(f"unknown identity kind {kind!r}")
    if kind in ("cap", "impl", "iface"):
        if len(parts) != 1:
            raise IDError(f"{kind} ID takes one name")
        return f"{kind}:{_check_name(parts[0], kind)}"
    if kind in ("run", "goal"):
        if len(parts) != 1:
            raise IDError(f"{kind} ID takes one component")
        return f"{kind}:{_encode_component(parts[0])}"
    if kind == "repo":
        if len(parts) != 2:
            raise IDError("repo ID takes owner and name")
        return repo_id(parts[0], parts[1])
    if kind == "checkout":
        if len(parts) != 2:
            raise IDError("checkout ID takes repo and fingerprint")
        return checkout_id(parts[0], parts[1])
    if kind == "snapshot":
        if len(parts) != 2:
            raise IDError("snapshot ID takes checkout and graph hash")
        return snapshot_id(parts[0], parts[1])
    if len(parts) != 4:
        raise IDError("project ID takes repo, checkout, snapshot and key")
    return qualify(parts[0], parts[1], parts[2], parts[3])


def assert_unique_ids(ids: Iterable[str]) -> list[str]:
    """Return sorted canonical IDs, raising on duplicate canonical identity.

    Two declarations that normalize to the same canonical identity are a
    collision even if their raw spellings differ.
    """
    seen: dict[str, str] = {}
    for raw in ids:
        ident = parse_id(raw)
        previous = seen.get(ident.canonical)
        if previous is not None:
            raise IDCollisionError(
                f"identities {previous!r} and {raw!r} both canonicalize to "
                f"{ident.canonical!r}")
        seen[ident.canonical] = raw
    return sorted(seen)


def repo_id_from_origin(origin: str) -> str:
    """Derive ``repo:<owner>/<name>`` from a normalized Git origin URL.

    Supported: ``https``/``http``/``ssh``/``git``/``git+ssh`` URLs and the
    scp-like ``git@host:owner/name`` form.  The host and user are ignored;
    the path is lowercased and one trailing ``.git`` is removed.  Nested
    groups stay in the owner component (``group/sub/repo`` becomes
    ``repo:group%2Fsub/repo``).  Filesystem paths and ``file:`` URLs are
    rejected: a checkout ID, not a path, represents a local copy.
    """
    if not isinstance(origin, str) or not origin.strip():
        raise IDError("origin URL must be a non-empty string")
    text = origin.strip()
    if any(ord(char) < 0x20 or char.isspace() for char in text):
        raise IDError("origin URL contains whitespace or control characters")
    if "://" in text:
        scheme, rest = text.split("://", 1)
        scheme = scheme.lower()
        if scheme not in {"http", "https", "ssh", "git", "git+ssh"}:
            raise IDError(f"unsupported origin scheme {scheme!r}")
        if "/" not in rest:
            raise IDError("origin URL has no repository path")
        _, _, path = rest.partition("/")
        path = "/" + path
    else:
        match = re.fullmatch(
            r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:(?P<path>[^:]+)", text)
        if match is None:
            raise IDError(
                "origin must be an http(s)/ssh/git URL, a scp-like "
                "git@host:path, or a supported remote; local paths are not "
                "stable repo identities")
        path = match.group("path")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = path.split("/")
    if len(segments) < 2 or any(not part or part in {".", ".."}
                                for part in segments):
        raise IDError(f"origin URL has no <owner>/<name> path: {origin!r}")
    try:
        lowered = [part.lower() for part in segments]
    except AttributeError as exc:  # pragma: no cover - segments are strings
        raise IDError("origin path is not text") from exc
    name = lowered[-1]
    owner = "/".join(lowered[:-1])
    if not re.fullmatch(r"[a-z0-9._-]+", name):
        raise IDError(f"origin repository name {name!r} is not lowercase ASCII")
    for part in lowered[:-1]:
        if not re.fullmatch(r"[a-z0-9._-]+", part):
            raise IDError(f"origin owner segment {part!r} is not lowercase ASCII")
    return repo_id(owner, name)


# ---------------------------------------------------------------------------
# Vocabulary and readiness
# ---------------------------------------------------------------------------


def freshness(value: str) -> str:
    """Validate a freshness value."""
    if value not in FRESHNESS:
        raise VocabularyError(
            f"invalid freshness {value!r}; expected one of {FRESHNESS}")
    return value


def evidence(value: str) -> str:
    """Validate an evidence kind."""
    if value not in EVIDENCE:
        raise VocabularyError(
            f"invalid evidence kind {value!r}; expected one of {EVIDENCE}")
    return value


def result(value: str) -> str:
    """Validate a criterion result."""
    if value not in RESULTS:
        raise VocabularyError(
            f"invalid result {value!r}; expected one of {RESULTS}")
    return value


def clock(value: str) -> str:
    """Validate a clock label.

    Durations use monotonic deltas; wall-clock time is only for cross-process
    ordering and is never enough to infer a dependency edge.
    """
    if value not in CLOCKS:
        raise VocabularyError(
            f"invalid clock {value!r}; expected one of {CLOCKS}")
    return value


def readiness(value: Any) -> bool | str:
    """Validate a readiness value: ``True``, ``False`` or ``"unknown"``.

    ``"unknown"`` is deliberately distinct from ``False`` and from a missing
    dimension.
    """
    if value is True or value is False:
        return value
    if value == READINESS_UNKNOWN:
        return READINESS_UNKNOWN
    raise VocabularyError(
        f"invalid readiness value {value!r}; expected true, false or 'unknown'")


def readiness_map(values: Mapping[str, Any]) -> dict[str, bool | str]:
    """Return all readiness dimensions, missing ones as ``"unknown"``."""
    if not isinstance(values, Mapping):
        raise VocabularyError("readiness must be a mapping")
    for dimension in values:
        if dimension not in READINESS_DIMENSIONS:
            raise VocabularyError(
                f"unknown readiness dimension {dimension!r}; expected one of "
                f"{READINESS_DIMENSIONS}")
    return {dimension: readiness(values.get(dimension, READINESS_UNKNOWN))
            for dimension in READINESS_DIMENSIONS}


def join_freshness(parent: str, child: str) -> str:
    """Join parent and child freshness without upgrading either.

    Order of dominance: ``absent`` > ``unknown`` > ``stale`` > ``current``.
    A ``current`` child of a stale or unknown parent can never be returned as
    current.
    """
    parent = freshness(parent)
    child = freshness(child)
    if "absent" in (parent, child):
        return "absent"
    if "unknown" in (parent, child):
        return "unknown"
    if "stale" in (parent, child):
        return "stale"
    return "current"


# ---------------------------------------------------------------------------
# Fact and relation records
# ---------------------------------------------------------------------------


def _check_source(value: Any, name: str = "source") -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise SchemaError(f"{name} must be a non-empty string of <=128 chars")
    if any(ord(char) < 0x20 for char in value):
        raise SchemaError(f"{name} contains control characters")
    return value


def _check_observed_at(value: Any) -> int | str:
    if isinstance(value, bool):
        raise SchemaError("observed_at must be an injected timestamp, not bool")
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise IntOutOfRangeError("observed_at is outside signed 64-bit range")
        return value
    if isinstance(value, str):
        if not value or len(value) > 64 or any(ord(c) < 0x20 for c in value):
            raise SchemaError("observed_at string is malformed or too long")
        return value
    raise SchemaError("observed_at must be an integer or string; clocks are "
                      "injected by the caller")


def _check_scope(scope: Iterable[str]) -> tuple[str, ...]:
    if isinstance(scope, str):
        raise SchemaError("scope must be an iterable of strings, not a string")
    values = tuple(scope)
    if len(values) > MAX_SCOPE_ITEMS:
        raise BoundsError(f"scope exceeds {MAX_SCOPE_ITEMS} entries")
    checked = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise SchemaError("scope entries must be non-empty strings <=128 chars")
        if any(ord(char) < 0x20 for char in value):
            raise SchemaError("scope entries contain control characters")
        checked.append(value)
    return tuple(sorted(dict.fromkeys(checked)))


def _check_optional_version(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise SchemaError(f"{name} must be a bounded string or null")
    if any(ord(char) < 0x20 for char in value):
        raise SchemaError(f"{name} contains control characters")
    return value


@dataclass(frozen=True)
class Fact:
    """One assertion with provenance, freshness, evidence and scope."""

    subject: str
    value: Any
    source: str
    observed_at: int | str
    freshness: str
    evidence: str
    owner: str | None = None
    subject_version: str | None = None
    scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        subject = self.subject
        if not isinstance(subject, str) or not subject or len(subject) > MAX_TEXT:
            raise SchemaError("fact subject must be a bounded non-empty string")
        if any(ord(char) < 0x20 for char in subject):
            raise SchemaError("fact subject contains control characters")
        _check_source(self.source)
        _check_observed_at(self.observed_at)
        freshness(self.freshness)
        evidence(self.evidence)
        _check_optional_version(self.owner, "owner")
        _check_optional_version(self.subject_version, "subject_version")
        object.__setattr__(self, "scope", _check_scope(self.scope))
        _bounded_value(self.value, path="fact.value", max_bytes=MAX_FACT_BYTES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FACT_VERSION,
            "subject": self.subject,
            "value": self.value,
            "source": self.source,
            "owner": self.owner,
            "observed_at": self.observed_at,
            "subject_version": self.subject_version,
            "freshness": self.freshness,
            "evidence": self.evidence,
            "scope": list(self.scope),
        }


@dataclass(frozen=True)
class Relation:
    """A typed edge between two subjects with provenance and evidence."""

    type: str
    from_id: str
    to_id: str
    source: str
    observed_at: int | str
    freshness: str
    evidence: str
    owner: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        relation_type = self.type
        if (not isinstance(relation_type, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", relation_type)):
            raise SchemaError(
                "relation type must be lowercase [a-z][a-z0-9_.-]{0,63}")
        for name in ("from_id", "to_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
                raise SchemaError(f"relation {name} must be a bounded string")
            if any(ord(char) < 0x20 for char in value):
                raise SchemaError(f"relation {name} contains control characters")
        _check_source(self.source)
        _check_observed_at(self.observed_at)
        freshness(self.freshness)
        evidence(self.evidence)
        _check_optional_version(self.owner, "owner")
        _check_optional_version(self.from_version, "from_version")
        _check_optional_version(self.to_version, "to_version")
        object.__setattr__(self, "scope", _check_scope(self.scope))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RELATION_VERSION,
            "type": self.type,
            "from": self.from_id,
            "to": self.to_id,
            "source": self.source,
            "owner": self.owner,
            "observed_at": self.observed_at,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "freshness": self.freshness,
            "evidence": self.evidence,
            "scope": list(self.scope),
        }


# ---------------------------------------------------------------------------
# Service errors and truncation envelopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceError:
    """A typed service failure: a vocabulary code plus bounded detail."""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in SERVICE_ERROR_CODES:
            raise ContractError(
                f"unknown service error code {self.code!r}",
                code="contract.invalid_service_error")
        if (not isinstance(self.message, str) or not self.message
                or len(self.message) > MAX_ERROR_MESSAGE
                or "\n" in self.message or "\r" in self.message):
            raise ContractError(
                "service error message must be one bounded line",
                code="contract.invalid_service_error")
        if self.retryable is not True and self.retryable is not False:
            raise ContractError(
                "service error retryable must be a bool",
                code="contract.invalid_service_error")
        if not isinstance(self.details, Mapping):
            raise ContractError(
                "service error details must be a mapping",
                code="contract.invalid_service_error")
        _bounded_value(dict(self.details), path="service_error.details",
                       max_bytes=MAX_DETAILS_BYTES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Truncation:
    """The bounded omission envelope."""

    truncated: bool = False
    omitted: int = 0
    next_cursor: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.truncated is not True and self.truncated is not False:
            raise TruncationError("truncated must be a bool")
        if isinstance(self.omitted, bool) or not isinstance(self.omitted, int):
            raise TruncationError("omitted must be an integer")
        if self.omitted < 0 or self.omitted > INT64_MAX:
            raise TruncationError("omitted must be a non-negative integer")
        if self.next_cursor is not None:
            if (not isinstance(self.next_cursor, str) or not self.next_cursor
                    or len(self.next_cursor) > MAX_CURSOR):
                raise TruncationError(
                    f"next_cursor must be a non-empty string <= {MAX_CURSOR}")
        if not self.truncated:
            if self.omitted != 0 or self.next_cursor is not None \
                    or self.reason is not None:
                raise TruncationError(
                    "an untruncated envelope has omitted=0, no cursor and no "
                    "reason")
            return
        if self.reason not in TRUNCATION_REASONS:
            raise TruncationError(
                f"truncated envelope needs a reason from "
                f"{sorted(TRUNCATION_REASONS)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "truncated": self.truncated,
            "omitted": self.omitted,
            "next_cursor": self.next_cursor,
            "reason": self.reason,
        }


def truncation(truncated: bool = False, *, omitted: int = 0,
               next_cursor: str | None = None,
               reason: str | None = None) -> Truncation:
    """Build a validated :class:`Truncation` envelope."""
    return Truncation(truncated=truncated, omitted=omitted,
                      next_cursor=next_cursor, reason=reason)


def validate_truncation(value: Mapping[str, Any] | Truncation) -> dict[str, Any]:
    """Validate a truncation mapping (or value) and return its mapping."""
    if isinstance(value, Truncation):
        return value.to_dict()
    if not isinstance(value, Mapping):
        raise TruncationError("truncation envelope must be a mapping")
    allowed = {"truncated", "omitted", "next_cursor", "reason"}
    unknown = [key for key in value if key not in allowed]
    if unknown:
        raise TruncationError(f"unknown truncation fields: {unknown}")
    return Truncation(
        truncated=value.get("truncated", False),
        omitted=value.get("omitted", 0),
        next_cursor=value.get("next_cursor"),
        reason=value.get("reason"),
    ).to_dict()


__all__ = [
    "BoundsError", "CLOCKS", "CanonicalJSONError", "ContractError",
    "CURRENT_VERSION", "DuplicateKeyError", "ERROR_CODES", "EVIDENCE",
    "EXTENSION_PREFIX",
    "FACT_VERSION", "FRESHNESS", "Fact", "IDCollisionError", "IDError",
    "ID_KINDS", "INT64_MAX", "INT64_MIN", "Identity", "IntOutOfRangeError",
    "InvalidKeyError", "InvalidUnicodeError", "MAP_DEFAULT_MAX_BYTES",
    "MAP_DEFAULT_MAX_DEPTH", "MAP_DEFAULT_MAX_NODES", "READINESS_DIMENSIONS",
    "READINESS_UNKNOWN", "RELATION_VERSION", "RESULTS", "Relation",
    "SENSITIVITIES", "SCHEMA_FIELDS", "SERVICE_ERROR_CODES", "SUPPORTED_MAJOR",
    "SchemaError", "ServiceError", "TRANSPORT_FIELDS", "TRUNCATION_REASONS",
    "Truncation", "TruncationError", "UnknownFieldError",
    "UnsupportedVersionError", "VersionError", "VocabularyError",
    "assert_unique_ids", "canonical_id", "canonical_json", "checkout_id",
    "clock", "content_hash", "evidence", "format_id", "freshness",
    "instance_hash",
    "join_freshness", "local_key", "normalize_version", "parse_id",
    "parse_version", "qualify", "readiness", "readiness_map", "repo_id",
    "repo_id_from_origin", "result", "schema_fingerprint", "snapshot_id",
    "truncation", "validate_instance", "validate_schema",
    "validate_truncation",
]
