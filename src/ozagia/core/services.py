"""Thin, versioned service envelopes validated by the v1 contracts.

Every WP2 service result has one shape::

    {schema, service, version, status, data, error, provenance,
     side_effect, started_at, duration_ms, truncated}

``status`` is ``ok`` or ``error`` and is consistent with ``error``: an error
envelope has a validated :class:`ozagia.core.contracts.ServiceError`, an ok
envelope has none.  ``data`` and ``error`` may coexist when a service returns
partial data and still reports the failure.

This module is deliberately small and pure: it opens no file, imports no
target project, opens no socket, calls no model and starts no process.  A
caller injects the clock and observations; the only clock read is the
monotonic default behind :class:`Deadline`, used for timeout enforcement and
never written into provenance.

Timeouts
--------

A :class:`Deadline` is owned by the caller.  It is checked before a call
starts; an expired deadline does not invoke the operation at all.  The call
runs **once** and is never retried, so a timeout cannot repeat a side effect.
A deadline cannot interrupt a blocking operation: :func:`call` checks after
the operation returns only to record the duration, and a completed operation's
result is kept even when it overran.  Callers that need interruption must
bound their own I/O.  ``timeout_ms=0`` is valid and refuses to start.

Canonical form and hashing
--------------------------

The envelope is canonical JSON in the WP0 sense: no floats, only signed
64-bit integers, sorted keys, compact separators, UTF-8.  ``envelope_hash``
is SHA-256 over the canonical envelope.  ``started_at`` and ``duration_ms``
are caller-supplied/injected, so a caller that needs a stable hash supplies
stable values; this module never invents a timestamp or a zero duration.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, NoReturn

from ozagia.core import contracts as C
from ozagia.core.capabilities import SIDE_EFFECTS

SERVICE_NAMESPACE = "ozagia.service"
SERVICE_VERSION = "1.0"
STATUSES = ("ok", "error")

MAX_SERVICE_NAME = 128
MAX_DATA_BYTES = 192 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_PROVENANCE_SOURCES = 32
MAX_INPUTS = 64
MAX_DURATION_MS = C.INT64_MAX

ENVELOPE_FIELDS = (
    "schema", "service", "version", "status", "data", "error", "provenance",
    "side_effect", "started_at", "duration_ms", "truncated",
)

_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ServiceFailure(Exception):
    """A typed service failure carrying a validated contracts error."""

    def __init__(self, error: C.ServiceError) -> None:
        if not isinstance(error, C.ServiceError):
            raise TypeError("ServiceFailure needs a contracts.ServiceError")
        self.error = error
        super().__init__(error.message)

    def to_dict(self) -> dict[str, Any]:
        return self.error.to_dict()


def fail(code: str, message: str, *, retryable: bool = False,
         details: Mapping[str, Any] | None = None) -> NoReturn:
    """Raise :class:`ServiceFailure` with a validated code and bounded line."""
    raise ServiceFailure(C.ServiceError(
        code=code, message=_line(message), retryable=retryable,
        details=dict(details or {})))


def _line(text: Any, limit: int = C.MAX_ERROR_MESSAGE) -> str:
    value = " ".join(str(text).split())
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value[:limit]


_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)


def _check_service(value: Any) -> str:
    if not isinstance(value, str) or not _SERVICE_RE.fullmatch(value):
        raise C.SchemaError(
            "service must be lowercase dotted [a-z0-9_.-], at most 128 chars")
    return value


def _check_side_effect(value: Any) -> str:
    if value not in SIDE_EFFECTS:
        raise C.VocabularyError(
            f"invalid side-effect class {value!r}; expected one of "
            f"{SIDE_EFFECTS}")
    return str(value)


def _check_started_at(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise C.SchemaError("started_at must be an injected integer or string")
    if isinstance(value, int):
        if not C.INT64_MIN <= value <= C.INT64_MAX:
            raise C.IntOutOfRangeError("started_at is outside signed 64-bit")
        return value
    if isinstance(value, str):
        if not value or len(value) > 64 or any(ord(c) < 0x20 for c in value):
            raise C.SchemaError("started_at string is malformed or too long")
        return value
    raise C.SchemaError("started_at must be an integer, string or null")


def _check_duration(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise C.SchemaError("duration_ms must be an integer or null")
    if value < 0 or value > MAX_DURATION_MS:
        raise C.BoundsError("duration_ms must be between 0 and INT64_MAX")
    return value


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One provenance source with the contract vocabulary attached."""

    source: str
    observed_at: int | str | None = None
    freshness: str = "current"
    evidence: str = "observed"

    def __post_init__(self) -> None:
        if (not isinstance(self.source, str) or not self.source
                or len(self.source) > 128
                or any(ord(c) < 0x20 for c in self.source)):
            raise C.SchemaError(
                "provenance source must be a bounded non-empty string")
        _check_started_at(self.observed_at)
        C.freshness(self.freshness)
        C.evidence(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "evidence": self.evidence,
        }


def provenance(sources: Iterable[Source] = (), *,
               inputs: Iterable[str] = ()) -> dict[str, Any]:
    """Build the bounded, sorted provenance mapping used by an envelope."""
    rows: dict[str, dict[str, Any]] = {}
    count = 0
    for item in sources:
        if not isinstance(item, Source):
            raise C.SchemaError("provenance sources must be services.Source")
        count += 1
        if count > MAX_PROVENANCE_SOURCES:
            raise C.BoundsError(
                f"provenance exceeds {MAX_PROVENANCE_SOURCES} sources")
        rows[C.canonical_json(item.to_dict())] = item.to_dict()
    checked_inputs: list[str] = []
    for value in inputs:
        if not isinstance(value, str) or not value or len(value) > C.MAX_TEXT:
            raise C.SchemaError("provenance inputs must be bounded strings")
        if any(ord(c) < 0x20 for c in value):
            raise C.SchemaError("provenance inputs contain control characters")
        checked_inputs.append(value)
    if len(set(checked_inputs)) > MAX_INPUTS:
        raise C.BoundsError(f"provenance exceeds {MAX_INPUTS} inputs")
    return {
        "sources": [rows[key] for key in sorted(rows)],
        "inputs": sorted(set(checked_inputs)),
    }


def _validate_provenance(value: Any) -> dict[str, Any]:
    if value is None:
        return {"sources": [], "inputs": []}
    if not isinstance(value, Mapping):
        raise C.SchemaError("provenance must be a mapping")
    unknown = sorted(set(value) - {"sources", "inputs"})
    if unknown:
        raise C.UnknownFieldError(f"unknown provenance fields: {unknown}")
    raw_sources = value.get("sources") or []
    if isinstance(raw_sources, (str, bytes)) or not isinstance(
            raw_sources, Iterable):
        raise C.SchemaError("provenance.sources must be a list")
    sources: list[Source] = []
    for item in raw_sources:
        if not isinstance(item, Mapping):
            raise C.SchemaError("each provenance source must be a mapping")
        unknown = sorted(set(item) - {"source", "observed_at", "freshness",
                                      "evidence"})
        if unknown:
            raise C.UnknownFieldError(
                f"unknown provenance source fields: {unknown}")
        sources.append(Source(
            source=item.get("source"),
            observed_at=item.get("observed_at"),
            freshness=item.get("freshness", "current"),
            evidence=item.get("evidence", "observed"),
        ))
    return provenance(sources, inputs=value.get("inputs") or ())


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def monotonic_ms() -> int:
    """Milliseconds from the process monotonic clock (timeout enforcement)."""
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True)
class Deadline:
    """A caller-owned timeout measured on an injected monotonic clock."""

    timeout_ms: int
    clock: Callable[[], int] = monotonic_ms
    started_ms: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.timeout_ms, bool) or not isinstance(
                self.timeout_ms, int):
            raise C.SchemaError("timeout_ms must be an integer")
        if self.timeout_ms < 0 or self.timeout_ms > MAX_DURATION_MS:
            raise C.BoundsError("timeout_ms must be between 0 and INT64_MAX")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if isinstance(self.started_ms, bool) or not isinstance(
                self.started_ms, int):
            raise C.SchemaError("started_ms must be an integer")

    @classmethod
    def after(cls, timeout_ms: int, *,
              clock: Callable[[], int] | None = None) -> "Deadline":
        tick = clock or monotonic_ms
        return cls(timeout_ms=timeout_ms, clock=tick, started_ms=int(tick()))

    def elapsed_ms(self) -> int:
        return max(0, int(self.clock()) - self.started_ms)

    def remaining_ms(self) -> int:
        return max(0, self.timeout_ms - self.elapsed_ms())

    def expired(self) -> bool:
        return self.elapsed_ms() >= self.timeout_ms

    def check(self, *, what: str = "service call") -> None:
        if self.expired():
            fail("timeout", f"{what} exceeded its {self.timeout_ms} ms deadline")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _validate_error(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, C.ServiceError):
        return value.to_dict()
    if not isinstance(value, Mapping):
        raise C.ContractError(
            "error must be a contracts ServiceError mapping or null",
            code="contract.invalid_service_error")
    unknown = sorted(set(value) - {"code", "message", "retryable", "details"})
    if unknown:
        raise C.ContractError(
            f"unknown service error fields: {unknown}",
            code="contract.invalid_service_error")
    error = C.ServiceError(
        code=value.get("code"),
        message=value.get("message"),
        retryable=value.get("retryable", False),
        details=value.get("details") or {},
    )
    return error.to_dict()


def _validate_data(value: Any) -> Any:
    encoded = C.canonical_json(value)
    if len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
        raise C.BoundsError(f"data exceeds {MAX_DATA_BYTES} canonical bytes")
    return value


def _check_envelope_fields(record: Mapping[str, Any]) -> None:
    allowed = set(ENVELOPE_FIELDS)
    for key in record:
        if not isinstance(key, str):
            raise C.UnknownFieldError(
                "service envelope field names must be strings")
        if key in allowed or key.startswith(C.EXTENSION_PREFIX):
            continue
        raise C.UnknownFieldError(f"unknown service envelope field {key!r}")


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an envelope and return a normalized copy."""
    if not isinstance(record, Mapping):
        raise C.SchemaError("service envelope must be a mapping")
    _check_envelope_fields(record)
    missing = [name for name in ENVELOPE_FIELDS if name not in record]
    if missing:
        raise C.SchemaError(f"service envelope is missing fields: {missing}")

    schema = record["schema"]
    if not isinstance(schema, Mapping):
        raise C.SchemaError("schema must be a mapping")
    unknown = sorted(set(schema) - {"namespace", "version"})
    if unknown:
        raise C.UnknownFieldError(f"unknown schema fields: {unknown}")
    if schema.get("namespace") != SERVICE_NAMESPACE:
        raise C.SchemaError(
            f"schema namespace must be {SERVICE_NAMESPACE!r}")
    version = C.normalize_version(schema.get("version"))
    if version != SERVICE_VERSION:
        raise C.UnsupportedVersionError(
            f"service envelope schema version {version} is not supported")

    status = record["status"]
    if status not in STATUSES:
        raise C.VocabularyError(
            f"invalid status {status!r}; expected one of {STATUSES}")
    error = _validate_error(record["error"])
    if (status == "error") != (error is not None):
        raise C.SchemaError(
            "status 'error' must carry an error, and no other status may")

    normalized: dict[str, Any] = {
        "schema": {"namespace": SERVICE_NAMESPACE, "version": SERVICE_VERSION},
        "service": _check_service(record["service"]),
        "version": C.normalize_version(record["version"]),
        "status": status,
        "data": _validate_data(record["data"]),
        "error": error,
        "provenance": _validate_provenance(record["provenance"]),
        "side_effect": _check_side_effect(record["side_effect"]),
        "started_at": _check_started_at(record["started_at"]),
        "duration_ms": _check_duration(record["duration_ms"]),
        "truncated": C.validate_truncation(record["truncated"]),
    }
    for key in sorted(record):
        if key.startswith(C.EXTENSION_PREFIX):
            normalized[key] = record[key]
    encoded = C.canonical_json(normalized)
    if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise C.BoundsError(
            f"service envelope exceeds {MAX_ENVELOPE_BYTES} canonical bytes")
    return normalized


def build(*, service: str, version: str = SERVICE_VERSION,
          status: str = "ok", data: Any = None,
          error: C.ServiceError | Mapping[str, Any] | None = None,
          provenance: Mapping[str, Any] | None = None,
          side_effect: str = "none", started_at: int | str | None = None,
          duration_ms: int | None = None,
          truncated: C.Truncation | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build and validate one service envelope."""
    return validate({
        "schema": {"namespace": SERVICE_NAMESPACE, "version": SERVICE_VERSION},
        "service": service,
        "version": version,
        "status": status,
        "data": data,
        "error": error,
        "provenance": _validate_provenance(provenance),
        "side_effect": side_effect,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "truncated": (truncated if truncated is not None
                      else C.truncation()),
    })


def ok(service: str, data: Any, **fields: Any) -> dict[str, Any]:
    """A successful envelope; ``status`` is fixed."""
    fields.pop("status", None)
    return build(service=service, status="ok", data=data, **fields)


def failed(service: str, error: C.ServiceError,
           **fields: Any) -> dict[str, Any]:
    """An error envelope carrying a validated :class:`ServiceError`."""
    fields.pop("status", None)
    return build(service=service, status="error", data=fields.pop("data", None),
                 error=error, **fields)


def envelope_json(record: Mapping[str, Any]) -> str:
    """Canonical JSON for a validated envelope."""
    return C.canonical_json(validate(record))


def envelope_hash(record: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical envelope."""
    return C.content_hash(validate(record))


def envelope_size(record: Mapping[str, Any]) -> int:
    """Canonical UTF-8 byte size of a validated envelope."""
    return len(envelope_json(record).encode("utf-8"))


# ---------------------------------------------------------------------------
# Bounded invocation
# ---------------------------------------------------------------------------


def call(service: str, operation: Callable[[], Any], *,
         deadline: Deadline, version: str = SERVICE_VERSION,
         side_effect: str = "none",
         provenance: Mapping[str, Any] | None = None,
         truncated: C.Truncation | Mapping[str, Any] | None = None
         ) -> dict[str, Any]:
    """Run ``operation`` at most once under a caller-owned deadline.

    An expired deadline returns a ``timeout`` envelope without calling the
    operation.  A :class:`ServiceFailure` becomes its typed error.  Any other
    exception becomes a bounded ``internal`` error naming only the exception
    type, so a message cannot smuggle a secret into the envelope.
    """
    if not isinstance(deadline, Deadline):
        raise TypeError("deadline must be a services.Deadline")
    if not callable(operation):
        raise TypeError("operation must be callable")
    fields = {
        "version": version,
        "side_effect": side_effect,
        "provenance": provenance,
        "truncated": truncated,
    }
    try:
        deadline.check(what=f"{service} call")
    except ServiceFailure as exc:
        return failed(service, exc.error, started_at=deadline.started_ms,
                      duration_ms=0, **fields)
    started = int(deadline.clock())
    try:
        result = operation()
    except ServiceFailure as exc:
        return failed(service, exc.error, started_at=started,
                      duration_ms=max(0, int(deadline.clock()) - started),
                      **fields)
    except C.ContractError as exc:
        error = C.ServiceError(
            "invalid_value", f"contract violation: {exc.code}",
            details={"contract_code": exc.code})
        return failed(service, error, started_at=started,
                      duration_ms=max(0, int(deadline.clock()) - started),
                      **fields)
    except Exception as exc:  # noqa: BLE001 - the boundary must not leak text
        error = C.ServiceError(
            "internal",
            f"{type(exc).__name__} while executing {service}")
        return failed(service, error, started_at=started,
                      duration_ms=max(0, int(deadline.clock()) - started),
                      **fields)
    try:
        return ok(service, result, started_at=started,
                  duration_ms=max(0, int(deadline.clock()) - started),
                  **fields)
    except C.ContractError as exc:
        return _invalid_result(service, exc, started=started,
                               deadline=deadline, fields=fields)


def _invalid_result(service: str, exc: C.ContractError, *, started: int,
                    deadline: Deadline, fields: Mapping[str, Any]
                    ) -> dict[str, Any]:
    error = C.ServiceError(
        "invalid_value",
        f"the operation returned a value the envelope rejects: {exc.code}",
        details={"contract_code": exc.code})
    return failed(service, error, started_at=started,
                  duration_ms=max(0, int(deadline.clock()) - started),
                  **fields)


__all__ = [
    "Deadline", "ENVELOPE_FIELDS", "MAX_DATA_BYTES", "MAX_DURATION_MS",
    "MAX_ENVELOPE_BYTES", "MAX_INPUTS", "MAX_PROVENANCE_SOURCES",
    "MAX_SERVICE_NAME", "SERVICE_NAMESPACE", "SERVICE_VERSION", "STATUSES",
    "ServiceFailure", "Source", "build", "call", "envelope_hash",
    "envelope_json", "envelope_size", "fail", "failed", "monotonic_ms", "ok",
    "provenance", "validate",
]
