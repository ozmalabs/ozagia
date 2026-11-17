"""Read-only model-facing maps for the Ozagia self-graph (M3).

The package is standard-library only and never imports a target project,
starts a daemon, runs a test, calls a model or writes outside
``<root>/.ozagia/``.  It exposes four bounded map types:

``graph``
    a slice of the existing DevMemory snapshot;
``outline``
    signatures/headings from an existing outline, never source bodies;
``capabilities``
    the WP1 declared catalogue joined to observed surfaces;
``work``
    the WP2 goal/criterion/run/test/environment evidence join.

A map is data, not authority: host :class:`HostPolicy` checks happen before a
projection, negotiation only selects ``plain`` or ``structured`` rendering,
and the existing owners remain authoritative for every fact and every action.

Public entry points
-------------------

``request_map(request, policy, ...)``
    validate, negotiate, project and return one response (typed errors are
    returned as ``ok: false``).
``HostPolicy``
    the host-owned read scope (root, session, model, maps, paths, budgets).
``make_request`` / ``validate_request`` / ``validate_map``
    canonical request and map-instance contracts.
``CacheStore`` / ``cache_key``
    inspectable cache metadata under ``<root>/.ozagia/model_api/cache``.
``negotiate``
    the deterministic representation matrix and redacted trace.
"""

from __future__ import annotations

from ozagia.model_api.cache import (
    CACHE_NAMESPACE,
    CACHE_VERSION,
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    CacheStore,
    cache_key,
)
from ozagia.model_api.errors import ModelAPIError, error_payload
from ozagia.model_api.negotiate import (
    NEGOTIATION_NAMESPACE,
    NEGOTIATION_VERSION,
    Negotiation,
    negotiate as negotiate_request,
)
from ozagia.model_api.policy import HostPolicy, PolicyDecision, decide
from ozagia.model_api.project import (
    SourceView,
    project_capabilities,
    project_graph,
    project_outline,
    project_work,
    request_map,
)
from ozagia.model_api.schema import (
    DEFAULT_BUDGETS,
    DEFAULT_PAGE_ITEMS,
    FORMATS,
    HARD_BUDGETS,
    HARD_MAX_BYTES,
    HARD_MAX_DEPTH,
    HARD_MAX_ITEMS,
    MAP_LABELS,
    MAP_TYPES,
    MAP_VERSION,
    REQUEST_NAMESPACE,
    REQUEST_VERSION,
    RESPONSE_NAMESPACE,
    build_instance,
    decode_cursor,
    encode_cursor,
    make_request,
    map_facts,
    map_schema,
    map_schema_fingerprint,
    plain_view,
    render_plain,
    representation,
    schema_ref,
    structured_view,
    validate_map,
    validate_request,
)

__version__ = "1.0.0"

__all__ = [
    "CACHE_NAMESPACE", "CACHE_VERSION", "CacheStore", "DEFAULT_BUDGETS",
    "DEFAULT_PAGE_ITEMS", "FORMATS", "HARD_BUDGETS", "HARD_MAX_BYTES",
    "HARD_MAX_DEPTH", "HARD_MAX_ITEMS", "HostPolicy", "MAP_LABELS",
    "MAP_TYPES", "MAP_VERSION", "MAX_ENTRIES", "MAX_ENTRY_BYTES",
    "ModelAPIError", "NEGOTIATION_NAMESPACE", "NEGOTIATION_VERSION",
    "Negotiation", "PolicyDecision", "REQUEST_NAMESPACE", "REQUEST_VERSION",
    "RESPONSE_NAMESPACE", "SourceView", "__version__", "build_instance",
    "cache_key", "decode_cursor", "decide", "encode_cursor", "error_payload",
    "make_request", "map_facts", "map_schema", "map_schema_fingerprint",
    "negotiate_request", "plain_view", "project_capabilities", "project_graph",
    "project_outline", "project_work", "render_plain", "representation",
    "request_map", "schema_ref", "structured_view", "validate_map",
    "validate_request",
]
