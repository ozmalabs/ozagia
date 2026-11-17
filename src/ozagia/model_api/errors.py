"""Typed, fail-closed errors for the read-only model-facing API (M3).

Every failure is expressed with the v1 service-error vocabulary from
:mod:`ozagia.core.contracts` (``invalid_request``, ``forbidden``, ``stale``,
``unsupported``, ``budget_exceeded`` and so on) plus one bounded, stable
``reason`` string owned by this package.  The reason is what a host or a model
adapter can branch on; the service code keeps the wire contract compatible
with the M2 service envelope.

Messages are deliberately short and never include a source exception message,
a prompt, a raw log line or a secret value.  The original exception type is
kept as a reason only when it is safe to name.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ozagia.core import contracts as C

MAX_MESSAGE = C.MAX_ERROR_MESSAGE

# Finer-grained, stable model-API reasons.  They never replace the service
# code; they explain it without widening the shared contract vocabulary.
REASONS = frozenset({
    "action_conflict",
    "budget_over_hard_bound",
    "budget_over_policy",
    "cache_path_escape",
    "capability_not_found",
    "changed_base",
    "checkout_mismatch",
    "cross_scope",
    "cursor_scope_mismatch",
    "edit_failed",
    "evidence_unavailable",
    "format_not_allowed",
    "invalid_internal_state",
    "ledger_unavailable",
    "malformed_budget",
    "malformed_cursor",
    "malformed_proposal",
    "malformed_request",
    "malformed_scope",
    "malformed_target",
    "map_not_allowed",
    "map_not_found",
    "map_over_budget",
    "missing_data",
    "model_not_allowed",
    "node_mismatch",
    "outline_target_unsupported",
    "parse_failed",
    "path_escape",
    "policy_root_missing",
    "preview_mismatch",
    "preview_required",
    "proposal_over_budget",
    "proposal_schema_mismatch",
    "schema_mismatch",
    "scope_unverifiable",
    "session_not_allowed",
    "snapshot_mismatch",
    "snapshot_unverifiable",
    "source_absent",
    "source_unknown",
    "target_not_found",
    "unsupported_format",
    "unsupported_map",
    "unsupported_operation",
    "unsupported_version",
    "write_declined",
})

_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _line(message: Any, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(str(message).replace("\r", " ").replace("\n", " ").split())
    return text[:limit] or "request failed"


class ModelAPIError(ValueError):
    """A typed model-API failure with a service code and a stable reason.

    ``code`` must be one of :data:`ozagia.core.contracts.SERVICE_ERROR_CODES`;
    an unknown code is a programming error and becomes ``internal``.  The
    reason must be lowercase ASCII; an unknown reason is rejected rather than
    silently inventing vocabulary.
    """

    def __init__(self, message: str, *, code: str = "invalid_request",
                 reason: str = "malformed_request",
                 details: Mapping[str, Any] | None = None,
                 retryable: bool = False) -> None:
        if code not in C.SERVICE_ERROR_CODES:
            code = "internal"
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise ValueError(f"invalid model_api error reason {reason!r}")
        self.code = code
        self.reason = reason
        self.retryable = bool(retryable)
        self.details: dict[str, Any] = dict(details or {})
        try:
            self.service_error = C.ServiceError(
                code=code, message=_line(message), retryable=self.retryable,
                details=self.details)
        except C.ContractError:
            self.service_error = C.ServiceError(
                code=code, message="request failed", retryable=False)
        super().__init__(self.service_error.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.service_error.to_dict(),
            "reason": self.reason,
        }


def internal_error(exc: BaseException) -> ModelAPIError:
    """Convert an unexpected exception without exposing its message."""
    return ModelAPIError(
        f"internal model API failure ({type(exc).__name__})",
        code="internal", reason="invalid_internal_state")


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, ModelAPIError):
        return exc.reason
    return "invalid_internal_state"


def error_payload(exc: BaseException, *, trace: Mapping[str, Any] | None = None,
                  fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the stable error response for a model map request."""
    if isinstance(exc, ModelAPIError):
        failure = exc
    else:
        failure = internal_error(exc)
    return {
        "ok": False,
        "schema": {"namespace": "ozagia.model_api.response",
                   "version": "1.0"},
        "response_version": "1.0",
        "error": failure.service_error.to_dict(),
        "reason": failure.reason,
        "fallback": dict(fallback) if fallback else {
            "used": True,
            "representation": "plain",
            "reason": _reason_for(failure),
            "instruction": ("Use the existing plain MCP/CLI tools; no map was "
                            "produced for this request."),
        },
        "trace": dict(trace) if trace else {
            "trace_id": None, "redacted": True, "events": []},
        "authority": {
            "granted": False,
            "note": ("A map or an error response never grants permission; "
                     "host policy and existing authorization remain "
                     "authoritative."),
        },
    }


__all__ = [
    "MAX_MESSAGE", "ModelAPIError", "REASONS", "error_payload",
    "internal_error",
]
