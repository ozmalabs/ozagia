"""Model-facing maps (M3) and structured edit/interaction (M4).

The package is standard-library only and never imports a target project,
starts a daemon, runs a test or calls a model.  The M3 maps are read-only; the
only M3 write is cache metadata under ``<root>/.ozagia/model_api/cache``.  M4
adds one host-authorized edit path that writes the single requested target
file through the existing transaction (with its backup under
``<root>/.ozagia/edits/backups``) and an action ledger under
``<root>/.ozagia/model_api/actions``.  It exposes four bounded map types:

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
An edit proposal grants no authority either: a preview is free, and an apply
needs the host's :class:`EditAuthorization` plus the preview hash.

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

M4 adds exactly one structured edit proposal and one interaction request:

``handle_edit`` / ``parse_edit_proposal``
    preview-by-default validation of one ``replace_body`` proposal, with
    host authorization, base-hash and preview binding for an apply.
``EditAuthorization``
    the host's explicit permission to turn a preview into a write.
``action_evidence``
    the read-only join from an applied action and selected checks to the
    existing evidence collector; a claim is never a verdict.
``parse_interact_request`` / ``request_interact``
    one bounded more-context request routed through the existing map
    projectors, with no authority and no scope widening.
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
from ozagia.model_api.edit import (
    ACTION_NAMESPACE,
    ACTION_VERSION,
    EVIDENCE_NAMESPACE,
    EVIDENCE_VERSION,
    PROPOSAL_NAMESPACE,
    PROPOSAL_VERSION,
    SUPPORTED_OPERATIONS,
    EditAuthorization,
    EditProposal,
    action_evidence,
    apply_edit,
    handle_edit,
    parse_edit_proposal,
    preview_edit,
)
from ozagia.model_api.errors import ModelAPIError, error_payload
from ozagia.model_api.interact import (
    INTERACT_NAMESPACE,
    INTERACT_VERSION,
    InteractRequest,
    parse_interact_request,
    request_interact,
)
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
    "ACTION_NAMESPACE", "ACTION_VERSION", "CACHE_NAMESPACE", "CACHE_VERSION",
    "CacheStore", "DEFAULT_BUDGETS", "DEFAULT_PAGE_ITEMS", "EVIDENCE_NAMESPACE",
    "EVIDENCE_VERSION", "EditAuthorization", "EditProposal", "FORMATS",
    "HARD_BUDGETS", "HARD_MAX_BYTES", "HARD_MAX_DEPTH", "HARD_MAX_ITEMS",
    "HostPolicy", "INTERACT_NAMESPACE", "INTERACT_VERSION", "InteractRequest",
    "MAP_LABELS", "MAP_TYPES", "MAP_VERSION", "MAX_ENTRIES", "MAX_ENTRY_BYTES",
    "ModelAPIError", "NEGOTIATION_NAMESPACE", "NEGOTIATION_VERSION",
    "Negotiation", "PROPOSAL_NAMESPACE", "PROPOSAL_VERSION", "PolicyDecision",
    "REQUEST_NAMESPACE", "REQUEST_VERSION", "RESPONSE_NAMESPACE",
    "SUPPORTED_OPERATIONS", "SourceView", "__version__", "action_evidence",
    "apply_edit", "build_instance", "cache_key", "decode_cursor", "decide",
    "encode_cursor", "error_payload", "handle_edit", "make_request",
    "map_facts", "map_schema", "map_schema_fingerprint", "negotiate_request",
    "parse_edit_proposal", "parse_interact_request", "plain_view",
    "preview_edit", "project_capabilities", "project_graph", "project_outline",
    "project_work", "render_plain", "representation", "request_interact",
    "request_map", "schema_ref", "structured_view", "validate_map",
    "validate_request",
]
