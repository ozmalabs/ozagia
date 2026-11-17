"""Deterministic model/session/capability/version negotiation.

Negotiation decides *which representation the host returns*, not what the
model is allowed to do.  Configured support is an explicit host statement;
observed support is recorded only from a real observation (a parse, a
previous response or a caller-supplied measurement), never inferred from a
model family, a brand name or an acknowledgement.  Unknown is distinct from
unsupported and from supported.

Historical L1/L2 levels are **not** claimed, implemented or inferred.  The
only representations in v1 are ``plain`` (L0-style text) and ``structured``
(canonical JSON v1).  A fallback is explicit and leaves the supported facts
and omissions unchanged; it never becomes an empty successful structured
result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ozagia.core import contracts as C
from ozagia.model_api import schema
from ozagia.model_api.errors import ModelAPIError
from ozagia.model_api.policy import PolicyDecision

NEGOTIATION_NAMESPACE = "ozagia.model_api.negotiation"
NEGOTIATION_VERSION = "1.0"
SUPPORT = ("supported", "unsupported", "unknown")
REPRESENTATIONS = ("plain", "structured")
PROTOCOL_LEVELS = (
    "L0-style plain rendering and structured v1 only; historical L1/L2 "
    "support is not claimed or implemented",
)

FALLBACK_REASONS = frozenset({
    "configured_supported_observed_unknown",
    "configured_unknown",
    "configured_unsupported",
    "observed_unsupported",
    "policy_disallows_structured",
})


def _support(value: Any, name: str) -> str:
    if value not in SUPPORT:
        raise ModelAPIError(
            f"{name} must be one of {SUPPORT}", code="invalid_value",
            reason="malformed_request")
    return str(value)


def _configured(values: Mapping[str, Any] | None) -> dict[str, str]:
    out = {map_type: "unknown" for map_type in schema.MAP_TYPES}
    out["plain"] = "supported"
    if values:
        for name, value in values.items():
            if name not in out:
                raise ModelAPIError(
                    f"unknown negotiation capability {name!r}",
                    code="invalid_request", reason="malformed_request")
            out[name] = _support(value, f"configured[{name}]")
    return out


def _observed(values: Mapping[str, Any] | None) -> dict[str, str]:
    out = {map_type: "unknown" for map_type in schema.MAP_TYPES}
    if values:
        for name, value in values.items():
            if name not in out:
                raise ModelAPIError(
                    f"unknown negotiation capability {name!r}",
                    code="invalid_request", reason="malformed_request")
            out[name] = _support(value, f"observed[{name}]")
    return out


def _row(*, map_type: str, requested_format: str, policy: PolicyDecision,
         configured: str, observed: str, budgets: Mapping[str, int]) -> dict[str, Any]:
    fallback: dict[str, str] | None = None
    representation = "plain" if requested_format == "plain" else "structured"
    effective = "supported"
    reason: str | None = None
    if requested_format == "plain":
        representation = "plain"
        effective = "supported"
        reason = "plain_rendering"
    elif not policy.allow_structured:
        representation = "plain"
        effective = "fallback"
        reason = "policy_disallows_structured"
        fallback = {"representation": "plain", "reason": reason}
    elif configured == "unsupported":
        representation = "plain"
        effective = "fallback"
        reason = "configured_unsupported"
        fallback = {"representation": "plain", "reason": reason}
    elif configured == "unknown":
        representation = "plain"
        effective = "fallback"
        reason = "configured_unknown"
        fallback = {"representation": "plain", "reason": reason}
    elif observed == "unsupported":
        representation = "plain"
        effective = "fallback"
        reason = "observed_unsupported"
        fallback = {"representation": "plain", "reason": reason}
    elif observed == "unknown":
        # Configured support is an explicit host decision; structured output
        # is still produced, but observed support remains honestly unknown.
        representation = "structured"
        effective = "supported"
        reason = "configured_supported_observed_unknown"
    else:
        representation = "structured"
        effective = "supported"
        reason = "configured_and_observed_supported"
    return {
        "id": f"map:{map_type}",
        "version": schema.MAP_VERSION,
        "requested": map_type == policy.map_type,
        "configured": configured,
        "observed": observed,
        "effective": effective,
        "representation": representation,
        "fallback": fallback,
        "reason": reason,
        "budget": dict(budgets),
    }


@dataclass(frozen=True)
class Negotiation:
    """The negotiated representation, matrix and redacted trace."""

    format: str
    fallback: dict[str, Any] | None
    matrix: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    configured: dict[str, str] = field(default_factory=dict)
    observed: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": {"namespace": NEGOTIATION_NAMESPACE,
                       "version": NEGOTIATION_VERSION},
            "version": NEGOTIATION_VERSION,
            "requested": f"map:{self.trace.get('map')}",
            "format": self.format,
            "configured": dict(self.configured),
            "observed": dict(self.observed),
            "matrix": [dict(row) for row in self.matrix],
            "fallback": dict(self.fallback) if self.fallback else None,
            "protocol_levels": {
                "claimed": [],
                "implemented": ["plain", "structured-v1"],
                "note": PROTOCOL_LEVELS[0],
            },
            "trace": dict(self.trace),
        }


def negotiate(request: Mapping[str, Any], policy: PolicyDecision, *,
              configured_support: Mapping[str, Any] | None = None,
              observed_support: Mapping[str, Any] | None = None
              ) -> Negotiation:
    """Build the per-capability matrix and choose one representation."""
    configured = _configured(configured_support)
    observed = _observed(observed_support)
    requested_format = str(request.get("format") or "structured")
    rows = [
        _row(map_type=map_type, requested_format=requested_format,
             policy=policy, configured=configured[map_type],
             observed=observed[map_type], budgets=policy.budgets)
        for map_type in schema.MAP_TYPES
    ]
    selected = next(row for row in rows if row["requested"])
    selected_format = str(selected["representation"])
    fallback = selected["fallback"]
    if requested_format == "plain":
        fallback = None
    events = []
    for row in rows:
        events.append({
            "event": "negotiate",
            "capability": row["id"],
            "version": row["version"],
            "configured": row["configured"],
            "observed": row["observed"],
            "effective": row["effective"],
            "representation": row["representation"],
            "fallback": (row["fallback"]["reason"]
                         if row["fallback"] else None),
            "budget_hash": C.content_hash(row["budget"])[:12],
        })
    trace = {
        "trace_id": request.get("trace_id"),
        "redacted": True,
        "map": policy.map_type,
        "model": policy.model,
        "session": policy.session,
        "events": events,
        "note": ("trace metadata contains identities, versions, support "
                 "labels and budget hashes only; no prompt, raw log, model "
                 "output or secret value is recorded"),
    }
    return Negotiation(
        format=selected_format,
        fallback=dict(fallback) if fallback else None,
        matrix=rows,
        trace=trace,
        configured=configured,
        observed=observed,
    )


def configured_from_policy(policy: PolicyDecision,
                           values: Mapping[str, Any] | None = None
                           ) -> dict[str, str]:
    """Default configured support for a host adapter.

    This is deliberately not a model-family table.  A host that has explicit
    support configuration passes it; otherwise every structured map capability
    stays ``unknown`` and negotiates to plain.
    """
    configured = _configured(values)
    if not policy.allow_structured:
        configured["plain"] = "supported"
    return configured


__all__ = [
    "FALLBACK_REASONS", "NEGOTIATION_NAMESPACE", "NEGOTIATION_VERSION",
    "Negotiation", "PROTOCOL_LEVELS", "REPRESENTATIONS", "SUPPORT",
    "configured_from_policy", "negotiate",
]
