"""One bounded INTERACT-like request for more graph context (M4).

The M3 maps answer a host-selected question.  This module adds the model's
counterpart: one request for *one* bounded map page through an existing
read-only projector.  It is deliberately narrow:

* one of the existing map types (``graph``, ``outline``, ``capabilities`` or
  ``work``), one target and one page cursor;
* the same root/session/model/path/budget checks as any map request;
* no new projector, no graph mutation, no test run, no model call, no daemon
  start and no command execution;
* no authority.  A successful interaction returns data, and its response
  still says ``authority.granted=false``.  It cannot widen a path, budget,
  model, session, snapshot or map-type scope.

The request is parsed deterministically (bounded bytes, depth and nodes;
duplicate keys and floats refused) and translated into the v1 map request
contract.  It does not add a second graph reader or a second cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ozagia.core import contracts as C
from ozagia.core import evidence as E
from ozagia.model_api import policy as policy_mod
from ozagia.model_api import schema as schema_mod
from ozagia.model_api.errors import ModelAPIError, error_payload, internal_error
from ozagia.model_api.project import request_map

INTERACT_NAMESPACE = "ozagia.model_api.interact"
INTERACT_VERSION = "1.0"
MAX_INTERACT_BYTES = 32 * 1024
MAX_INTERACT_DEPTH = 6
MAX_INTERACT_NODES = 256
MAX_REQUEST_ID = 128
MAX_REASON = 300

_FIELDS = (
    "schema", "request_version", "request_id", "reason", "map", "scope",
    "target", "budgets", "cursor", "trace_id", "format", "strict",
)
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _fail(message: str, code: str, reason: str) -> ModelAPIError:
    return ModelAPIError(message, code=code, reason=reason)


class _DuplicateKey(ValueError):
    pass


class _FloatKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_float(_text: str) -> None:
    raise _FloatKey("floats are not accepted in an interaction request")


def _shape(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > MAX_INTERACT_NODES:
        raise _fail("the interaction request exceeds the node bound",
                    "budget_exceeded", "proposal_over_budget")
    if depth > MAX_INTERACT_DEPTH:
        raise _fail("the interaction request exceeds the depth bound",
                    "budget_exceeded", "proposal_over_budget")
    if isinstance(value, Mapping):
        for item in value.values():
            _shape(item, depth=depth + 1, count=count)
    elif isinstance(value, list):
        for item in value:
            _shape(item, depth=depth + 1, count=count)


def _load_request(payload: Any) -> Mapping[str, Any]:
    try:
        if isinstance(payload, Mapping):
            data = C.canonical_json(payload).encode("utf-8")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            data = bytes(payload)
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            raise _fail("an interaction request must be a mapping or JSON text",
                        "invalid_request", "malformed_request")
    except ModelAPIError:
        raise
    except C.ContractError as exc:
        raise _fail("the interaction request is not canonical JSON",
                    "invalid_value", "malformed_request") from exc
    except UnicodeEncodeError as exc:
        raise _fail("the interaction request is not valid UTF-8",
                    "invalid_value", "malformed_request") from exc
    if len(data) > MAX_INTERACT_BYTES:
        raise _fail(f"the interaction request exceeds {MAX_INTERACT_BYTES} "
                    "bytes", "budget_exceeded", "proposal_over_budget")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _fail("the interaction request must not carry a UTF-8 BOM",
                    "invalid_value", "malformed_request")
    try:
        text = data.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey,
            _FloatKey, ValueError, RecursionError) as exc:
        raise _fail("the interaction request is not deterministic JSON",
                    "invalid_request", "malformed_request") from exc
    if not isinstance(raw, Mapping):
        raise _fail("the interaction request must be a JSON object",
                    "invalid_request", "malformed_request")
    _shape(raw)
    return raw


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string", "invalid_request",
                    "malformed_request")
    text = value.strip()
    if not text or len(text) > 128 or not _LABEL_RE.fullmatch(text):
        raise _fail(f"{name} is not a bounded label", "invalid_request",
                    "malformed_request")
    return text


def _schema(raw: Mapping[str, Any]) -> None:
    ref = raw.get("schema")
    if not isinstance(ref, Mapping):
        raise _fail("schema must be a mapping", "invalid_request",
                    "malformed_request")
    for key in ref:
        if key not in ("namespace", "version") and not key.startswith(
                C.EXTENSION_PREFIX):
            raise _fail(f"schema has unknown field {key!r}", "unknown_field",
                        "malformed_request")
    if ref.get("namespace") != INTERACT_NAMESPACE:
        raise _fail("the interaction schema namespace is not the M4 namespace",
                    "invalid_request", "schema_mismatch")
    try:
        version = C.normalize_version(ref.get("version"))
    except C.ContractError as exc:
        raise _fail("the interaction schema version is unsupported",
                    "unsupported_version", "unsupported_version") from exc
    if version != INTERACT_VERSION:
        raise _fail("the interaction schema version is not 1.0",
                    "unsupported_version", "unsupported_version")


@dataclass(frozen=True)
class InteractRequest:
    """A validated interaction request translated to one map request."""

    request_id: str | None
    reason: str | None
    map_request: dict[str, Any]

    @property
    def map(self) -> str:
        return str(self.map_request["map"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": {"namespace": INTERACT_NAMESPACE,
                       "version": INTERACT_VERSION},
            "request_version": INTERACT_VERSION,
            "request_id": self.request_id,
            "reason": self.reason,
            "map": self.map,
            "map_request": dict(self.map_request),
        }


def parse_interact_request(payload: Any) -> InteractRequest:
    """Parse one bounded interaction request and translate it to a map request.

    Unknown fields are refused.  The map request is validated by
    :func:`ozagia.model_api.schema.validate_request`; host policy is applied
    later by :func:`request_interact` through the ordinary map path.
    """
    raw = _load_request(payload)
    for key in raw:
        if key not in _FIELDS and not key.startswith(C.EXTENSION_PREFIX):
            raise _fail(f"interaction request has unknown field {key!r}",
                        "unknown_field", "malformed_request")
    _schema(raw)
    if "request_version" not in raw:
        raise _fail("request_version is required", "invalid_request",
                    "malformed_request")
    try:
        version = C.normalize_version(raw.get("request_version"))
    except C.ContractError as exc:
        raise _fail("request_version is unsupported", "unsupported_version",
                    "unsupported_version") from exc
    if version != INTERACT_VERSION:
        raise _fail("request_version is not 1.0", "unsupported_version",
                    "unsupported_version")

    request_id = raw.get("request_id")
    if request_id is not None:
        request_id = _label(request_id, "request_id")
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise _fail("request_id has invalid characters", "invalid_request",
                        "malformed_request")
    reason = raw.get("reason")
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip():
            raise _fail("reason must be bounded text", "invalid_request",
                        "malformed_request")
        if len(reason.strip()) > MAX_REASON:
            raise _fail(f"reason exceeds {MAX_REASON} characters",
                        "invalid_request", "malformed_request")
        reason = E.redact_text(reason.strip(), MAX_REASON)
        if not reason or _CONTROL_RE.search(reason):
            raise _fail("reason is malformed or too long", "invalid_request",
                        "malformed_request")

    map_type = raw.get("map", "graph")
    if not isinstance(map_type, str):
        raise _fail("map must be a string", "invalid_request",
                    "malformed_request")
    scope = raw.get("scope", {})
    target = raw.get("target", {})
    budgets = raw.get("budgets", {})
    for name, value in (("scope", scope), ("target", target),
                        ("budgets", budgets)):
        if not isinstance(value, Mapping):
            raise _fail(f"{name} must be a mapping", "invalid_request",
                        "malformed_request")
    cursor = raw.get("cursor")
    trace_id = raw.get("trace_id")
    if trace_id is None:
        trace_id = request_id
    format_value = raw.get("format", "structured")
    strict = raw.get("strict", False)
    try:
        map_request = schema_mod.make_request(
            map_type,
            scope=scope,
            target=target,
            budgets=budgets,
            cursor=cursor,
            trace_id=trace_id,
            format=format_value,
            strict=strict,
        )
    except ModelAPIError:
        raise
    except Exception as exc:  # noqa: BLE001 - a malformed shape is not internal
        raise _fail("the interaction request cannot be translated to a map "
                    "request", "invalid_request", "malformed_request") from exc
    return InteractRequest(request_id=request_id, reason=reason,
                           map_request=map_request)


def _interaction(request: InteractRequest,
                 map_request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": {"namespace": INTERACT_NAMESPACE,
                   "version": INTERACT_VERSION},
        "request_version": INTERACT_VERSION,
        "request_id": request.request_id,
        "reason": request.reason,
        "map": request.map,
        "target": dict(map_request.get("target") or {}),
        "cursor": map_request.get("cursor"),
        "expanded": bool(map_request.get("target")),
        "note": ("one bounded read-only map page; request another interaction "
                 "for more context"),
        "authority": {
            "granted": False,
            "note": ("An interaction request and its map grant no permission "
                     "and cannot widen host scope."),
        },
    }


def _interaction_error(exc: ModelAPIError) -> dict[str, Any]:
    error = error_payload(exc, fallback={
        "used": True,
        "representation": "plain",
        "reason": exc.reason,
        "instruction": ("Use the existing plain MCP/CLI tools; no interaction "
                        "map was produced for this request."),
    })
    error["kind"] = "interact"
    error["interaction"] = {
        "schema": {"namespace": INTERACT_NAMESPACE,
                   "version": INTERACT_VERSION},
        "request_id": None,
        "reason": None,
        "map": None,
        "expanded": False,
        "authority": {"granted": False},
    }
    return error


def request_interact(
        payload: Any,
        policy: policy_mod.HostPolicy,
        *,
        configured_support: Mapping[str, Any] | None = None,
        observed_support: Mapping[str, Any] | None = None,
        cache_enabled: bool | None = None,
        capabilities: Any = None,
        observations: Mapping[str, Any] | None = None,
        evidence_sources: E.Sources | None = None,
        injected_mem: Any = None,
) -> dict[str, Any]:
    """Serve one interaction through the existing read-only map path.

    Typed failures are returned as ``ok: false``; a malformed request, a
    map the host did not enable, a cross-scope root/session/model, an
    out-of-policy budget or a stale cursor never becomes an empty success.
    """
    try:
        request = parse_interact_request(payload)
        response = request_map(
            request.map_request, policy,
            configured_support=configured_support,
            observed_support=observed_support,
            cache_enabled=cache_enabled,
            capabilities=capabilities,
            observations=observations,
            evidence_sources=evidence_sources,
            injected_mem=injected_mem,
        )
        if not isinstance(response, Mapping):
            raise _fail("the map projector returned no response", "internal",
                        "invalid_internal_state")
        decorated = dict(response)
        decorated["kind"] = "interact"
        decorated["interaction"] = _interaction(request, request.map_request)
        if "authority" not in decorated:
            decorated["authority"] = {
                "granted": False,
                "note": "an interaction response never grants permission",
            }
        return decorated
    except ModelAPIError as exc:
        return _interaction_error(exc)
    except Exception as exc:  # noqa: BLE001 - typed internal failure
        return _interaction_error(internal_error(exc))


__all__ = [
    "INTERACT_NAMESPACE", "INTERACT_VERSION", "InteractRequest",
    "MAX_INTERACT_BYTES", "MAX_INTERACT_DEPTH", "MAX_INTERACT_NODES",
    "MAX_REASON", "MAX_REQUEST_ID", "parse_interact_request", "request_interact",
]
