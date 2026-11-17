"""Optional Ocelot join for the read-only research projection.

This module adds one narrow, read-only operation over retained research
records: **source-anchored claim-shape analogies**.  Claims are selected from an
existing ``ozagia.research_projection`` document, grouped by their epistemic
class and by the exact identity of the sources they cite, and passed to the
optional Ocelot worker through ``ozagia.core.ocelot_code.cluster_findings``.
The worker compares one claim per run and returns candidate structural
analogies within each anchor.

The operation is deliberately not the review-finding reconciler in
``ozagia.research.ocelot_reconcile``: that reconciler groups findings made by
different reviewers about the *same* claim/reference target and preserves
dissent.  This adapter compares *different claims* and never records a support,
truth, consensus or derivation edge.

The result is a versioned section attached by :func:`attach_ocelot`; the base
projection is not modified when the join is omitted.  Ocelot is resolved and
executed through the existing optional path, and its absence is an explicit
``not_installed``/``unavailable``/``invalid`` state rather than an exception.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ozagia.core import contracts as C
from ozagia.core.evidence import redact_text

SCHEMA_NAMESPACE = "ozagia.ocelot.research_projection"
SCHEMA_VERSION = "1.0"
OPERATION_ID = "source_anchored_claim_analogy"
OPERATION_VERSION = "1.0"
ENGINE_ID = "ocelot:analogy_search"
REQUEST_SCHEMA = "ozagia.ocelot.research_projection.request.v1"
MODES = ("auto", "inprocess", "subprocess")

MAX_CLAIMS = 120
MAX_CANDIDATE_CLUSTERS = 40
MAX_EXACT_GROUPS = 40
MAX_ANALOGIES = 120
MAX_SECTION_BYTES = 64 * 1024
MAX_TEXT = 128
MAX_REASON = 300
MAX_DETAIL = 300
MAX_IDS = 16
MAX_VERSION = 64

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s\"'<>]+)")

_EPISTEMIC_BOUNDARY = {
    "empirical": (
        "recorded empirical_observation claims; no support or truth is asserted"
    ),
    "conditional": (
        "recorded hypothesis or inference claims; conditional, not observed or "
        "proved"
    ),
    "formal": (
        "recorded theorem or definition claims; not machine-checked and not a "
        "derivation"
    ),
    "unknown": "an unrecognised claim_type; never promoted",
    "cross_class_links": False,
    "evidence_kinds_merged": False,
}

_UNKNOWN = (
    "Ocelot does not verify claim truth, whether a source supports a claim, or "
    "whether a formal derivation is valid",
    "the worker returns structural clusters and analogies, not a calibrated "
    "score; no score is reported",
    "only claims with at least one cited source are compared; assumptions, "
    "evidence and sources are not clustered in this operation",
    "sources omitted from the projection, or without a recorded hash or "
    "reference, use a session-local identity; their cross-session identity is "
    "unknown",
    "a claim's source_ids may be truncated at 16 by the base projection; the "
    "adapter cannot see omitted source links",
    "the base projection may trim records or detail by its byte limit; the "
    "adapter only sees the records present in the supplied projection",
    "an unmanaged Ocelot checkout may not declare a version; the version is "
    "then reported as unknown",
    "the worker's structural comparison is not a semantic comparison and does "
    "not establish that claims say the same thing",
)

_DEFERRED = (
    "formal proof checking and Lean verification of derived claims",
    "an Ocelot version compatibility matrix; only an explicitly pinned version "
    "can be enforced",
    "calibrated confidence or coverage scoring from Ocelot output",
    "clustering assumptions, evidence records or sources as first-class shapes",
    "write-back, support-edge creation or claim promotion",
    "cross-repository or multi-checkout research merges",
)


class OcelotProjectionError(ValueError):
    """The attach request itself is malformed (not an Ocelot absence)."""


# ---------------------------------------------------------------------------
# Bounded helpers
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    return text[:limit]


def _line(value: Any, limit: int = MAX_REASON) -> str:
    return " ".join(_text(value, limit * 2).split())[:limit]


def _safe_detail(value: Any, *, paths: Sequence[Any] = ()) -> str:
    """A short, redacted error summary without checkout/root paths."""
    text = redact_text(value, MAX_DETAIL)
    for path in paths:
        if path:
            text = text.replace(str(path), "[path]")
    text = _ABS_PATH_RE.sub("[path]", text)
    text = re.sub(r"\[path\](?:/[^\s\"'<>]*)?", "[path]", text)
    text = re.sub(r"(?:\[path\])+", "[path]", text)
    return _line(text, MAX_DETAIL)


def _hash(value: Any) -> str:
    return C.content_hash(value)


def _normalised_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return text if _SHA256_RE.fullmatch(text) else None


_ENGINE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:+-]{1,128}\Z")


def _engine_token(value: Any, limit: int = MAX_TEXT) -> str | None:
    """A bounded identifier token; paths, spaces and secret shapes are dropped."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or not _ENGINE_TOKEN_RE.fullmatch(text):
        return None
    if redact_text(text, len(text) + 1) != text:
        return None
    return text


def _ids(value: Any, limit: int = MAX_IDS) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _relation_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(key, str) and key and isinstance(item, int) and not isinstance(item, bool):
            if item >= 0:
                out[_text(key, 64)] = item
        if len(out) >= MAX_IDS:
            break
    return dict(sorted(out.items()))


def _epistemic_class(claim_type: Any) -> str:
    normalized = _text(claim_type, 64).strip().casefold()
    if normalized == "empirical_observation":
        return "empirical"
    if normalized in {"theorem", "definition"}:
        return "formal"
    if normalized in {"hypothesis", "inference"}:
        return "conditional"
    return "unknown"


def _truncation(omitted: int, *, reason: str = "limit") -> dict[str, Any]:
    return C.truncation(omitted > 0, omitted=max(0, omitted),
                        reason=reason if omitted > 0 else None).to_dict()


# ---------------------------------------------------------------------------
# Claim, source and anchor views
# ---------------------------------------------------------------------------


def _claim_key(claim: Mapping[str, Any]) -> str | None:
    session = _text(claim.get("session"), 128)
    claim_id = _text(claim.get("id"), 128)
    if not session or not claim_id:
        return None
    return f"{session}/{claim_id}"


def _claim_view(claim: Mapping[str, Any]) -> dict[str, Any] | None:
    key = _claim_key(claim)
    if key is None:
        return None
    wording_sha256 = claim.get("wording_sha256")
    return {
        "key": key,
        "session": _text(claim.get("session"), 128),
        "id": _text(claim.get("id"), 128),
        "claim_type": _text(claim.get("claim_type"), 64) or "unknown",
        "status": _text(claim.get("status"), 64) or "unknown",
        "epistemic_class": _epistemic_class(claim.get("claim_type")),
        "support_state": _text(claim.get("support_state"), 64) or "unknown",
        "freshness": _text(claim.get("freshness"), 32) or "unknown",
        "model_candidate": claim.get("model_candidate") is True,
        "parsed_assertion": claim.get("parsed_assertion") is True,
        "semantics_verified": claim.get("semantics_verified") is True,
        "citation_count": _count(claim.get("citation_count")),
        "source_ids": sorted(_ids(claim.get("source_ids"))),
        "reference_ids": sorted(_ids(claim.get("reference_ids"))),
        "evidence_ids": sorted(_ids(claim.get("evidence_ids"))),
        "assumption_ids": sorted(_ids(claim.get("assumption_ids"))),
        "derived_from_ids": sorted(_ids(claim.get("derived_from_ids"))),
        "support_edge_count": _count(claim.get("support_edge_count")),
        "oppose_edge_count": _count(claim.get("oppose_edge_count")),
        "relation_counts": _relation_counts(claim.get("relation_counts")),
        "wording_sha256": (wording_sha256.lower()
                           if isinstance(wording_sha256, str)
                           and _SHA256_RE.fullmatch(wording_sha256.lower())
                           else None),
        "evidence": "declared",
        "truth_assessed": False,
    }


def _claim_fingerprint(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": view["key"],
        "claim_type": view["claim_type"],
        "status": view["status"],
        "epistemic_class": view["epistemic_class"],
        "support_state": view["support_state"],
        "freshness": view["freshness"],
        "model_candidate": view["model_candidate"],
        "parsed_assertion": view["parsed_assertion"],
        "semantics_verified": view["semantics_verified"],
        "citation_count": view["citation_count"],
        "source_ids": list(view["source_ids"]),
        "reference_ids": list(view["reference_ids"]),
        "evidence_ids": list(view["evidence_ids"]),
        "assumption_ids": list(view["assumption_ids"]),
        "derived_from_ids": list(view["derived_from_ids"]),
        "support_edge_count": view["support_edge_count"],
        "oppose_edge_count": view["oppose_edge_count"],
        "relation_counts": dict(view["relation_counts"]),
        "wording_sha256": view["wording_sha256"],
    }


def _source_index(projection: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    sources = projection.get("sources")
    if not isinstance(sources, (list, tuple)):
        return index
    for row in sources:
        if not isinstance(row, Mapping):
            continue
        session = _text(row.get("session"), 128)
        source_id = _text(row.get("id"), 128)
        if session and source_id:
            index[(session, source_id)] = row
    return index


def _source_identity(session: str, source_id: str,
                     sources: Mapping[tuple[str, str], Mapping[str, Any]]
                     ) -> tuple[str, str]:
    row = sources.get((session, source_id))
    if row is None:
        return f"unresolved:{session}/{source_id}", "unresolved"
    recorded = _normalised_hash(row.get("recorded_sha256"))
    if recorded is not None:
        return f"sha256:{recorded}", "recorded_sha256"
    observed = _normalised_hash(row.get("observed_sha256"))
    if observed is not None:
        return f"observed-sha256:{observed}", "observed_sha256"
    references = sorted(_ids(row.get("reference_ids")))
    if references:
        return f"reference:{session}/{references[0]}", "reference"
    return f"source:{session}/{source_id}", "local_record"


def _anchor_for(view: Mapping[str, Any],
                sources: Mapping[tuple[str, str], Mapping[str, Any]]
                ) -> dict[str, Any] | None:
    source_ids = list(view.get("source_ids") or [])
    if not source_ids:
        return None
    identities: list[str] = []
    kinds: set[str] = set()
    for source_id in source_ids:
        identity, kind = _source_identity(str(view["session"]), source_id, sources)
        identities.append(identity)
        kinds.add(kind)
    identities = sorted(set(identities))
    body = {
        "epistemic_class": view["epistemic_class"],
        "source_identities": identities,
    }
    return {
        "id": _hash(body),
        "epistemic_class": view["epistemic_class"],
        "source_identities": identities,
        "source_identity_kinds": sorted(kinds),
        "unresolved_sources": "unresolved" in kinds,
    }


def _anchor_view(anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": anchor["id"],
        "epistemic_class": anchor["epistemic_class"],
        "source_identities": list(anchor["source_identities"]),
        "source_identity_kinds": list(anchor["source_identity_kinds"]),
        "unresolved_sources": anchor["unresolved_sources"],
    }


def _structural_wording(view: Mapping[str, Any]) -> str:
    """A bounded structural summary; never the claim's wording."""
    return "; ".join((
        f"epistemic_class={view['epistemic_class']}",
        f"claim_type={view['claim_type']}",
        f"status={view['status']}",
        f"support_state={view['support_state']}",
        f"sources={len(view['source_ids'])}",
        f"evidence={len(view['evidence_ids'])}",
        f"assumptions={len(view['assumption_ids'])}",
        f"derived_from={len(view['derived_from_ids'])}",
        f"support_edges={view['support_edge_count']}",
        f"oppose_edges={view['oppose_edge_count']}",
        f"model_candidate={str(view['model_candidate']).lower()}",
    ))[:500]


def _payload_row(number: int, view: Mapping[str, Any],
                 anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": f"c{number:04d}",
        "run_id": f"r{number:04d}",
        "target_type": "ClaimAnchor",
        "target_id": anchor["id"],
        "family": "other",
        "severity": "note",
        "wording": _structural_wording(view),
    }


# ---------------------------------------------------------------------------
# Shared structural attributes
# ---------------------------------------------------------------------------


def _qualified(view: Mapping[str, Any], field: str) -> list[str]:
    return [f"{view['session']}/{value}" for value in view.get(field) or []]


def _shared(views: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def values(field: str) -> list[str]:
        return sorted({str(view.get(field)) for view in views})

    def intersection(field: str) -> list[str]:
        sets = [set(_qualified(view, field)) for view in views]
        if not sets:
            return []
        return sorted(set.intersection(*sets))

    claim_types = values("claim_type")
    statuses = values("status")
    support_states = values("support_state")
    return {
        "claim_types": claim_types,
        "statuses": statuses,
        "support_states": support_states,
        "freshness": values("freshness"),
        "model_candidate": sorted({bool(view.get("model_candidate"))
                                   for view in views}),
        "evidence_ids": intersection("evidence_ids"),
        "assumption_ids": intersection("assumption_ids"),
        "reference_ids": intersection("reference_ids"),
        "all_same_claim_type": len(claim_types) == 1,
        "all_same_status": len(statuses) == 1,
        "all_same_support_state": len(support_states) == 1,
    }


def _claim_summary(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": view["key"],
        "session": view["session"],
        "id": view["id"],
        "claim_type": view["claim_type"],
        "status": view["status"],
        "epistemic_class": view["epistemic_class"],
        "support_state": view["support_state"],
        "freshness": view["freshness"],
        "model_candidate": view["model_candidate"],
        "wording_sha256": view["wording_sha256"],
        "source_count": len(view["source_ids"]),
        "evidence_count": len(view["evidence_ids"]),
        "assumption_count": len(view["assumption_ids"]),
        "derived_from_count": len(view["derived_from_ids"]),
        "evidence": "declared",
        "truth_assessed": False,
    }


def _exact_group(number: int, anchor: Mapping[str, Any],
                 views: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "group_id": f"exact-{number:04d}",
        "origin": "exact",
        "candidate": False,
        "anchor": _anchor_view(anchor),
        "claims": [_claim_summary(view) for view in views],
        "size": len(views),
        "shared": _shared(views),
        "truth_assessed": False,
        "support_edges_created": False,
        "note": (
            "identical source anchor and epistemic class; this is a "
            "deterministic fallback group, not an Ocelot analogy or a support "
            "relation"
        ),
    }


def _candidate_cluster(number: int, anchor: Mapping[str, Any],
                       views: Sequence[Mapping[str, Any]],
                       analogy_count: int) -> dict[str, Any]:
    return {
        "cluster_id": f"ocelot-{number:04d}",
        "origin": "ocelot",
        "candidate": True,
        "anchor": _anchor_view(anchor),
        "claims": [_claim_summary(view) for view in views],
        "size": len(views),
        "shared": _shared(views),
        "analogy_count": analogy_count,
        "truth_assessed": False,
        "support_edges_created": False,
        "note": (
            "candidate structural analogy from Ocelot; not truth, support, "
            "derivation or consensus"
        ),
    }


def _analogy_view(left: Mapping[str, Any], right: Mapping[str, Any],
                  anchor: Mapping[str, Any], order_n: int | None) -> dict[str, Any]:
    return {
        "source": left["key"],
        "target": right["key"],
        "anchor": anchor["id"],
        "epistemic_class": anchor["epistemic_class"],
        "order_n": order_n,
        "shared": _shared([left, right]),
        "candidate": True,
        "truth_assessed": False,
        "support_edge_created": False,
    }


# ---------------------------------------------------------------------------
# Engine output filtering
# ---------------------------------------------------------------------------


def _filter_groups(raw: Any, by_id: Mapping[str, Mapping[str, Any]]
                   ) -> tuple[list[list[str]], dict[str, int]]:
    dropped = {
        "malformed": 0,
        "unknown_ids": 0,
        "singletons": 0,
        "split_clusters": 0,
        "subsets": 0,
    }
    groups: set[tuple[str, ...]] = set()
    if not isinstance(raw, (list, tuple)):
        return [], dropped
    for item in raw:
        if isinstance(item, Mapping):
            values = (item.get("members") or item.get("findings")
                      or item.get("claims") or [])
        elif isinstance(item, (list, tuple)):
            values = item
        else:
            dropped["malformed"] += 1
            continue
        by_anchor: dict[str, list[str]] = {}
        for value in values:
            payload_id = str(value)
            view = by_id.get(payload_id)
            if view is None:
                dropped["unknown_ids"] += 1
                continue
            by_anchor.setdefault(view["anchor"]["id"], []).append(payload_id)
        if len(by_anchor) > 1:
            dropped["split_clusters"] += 1
        for members in by_anchor.values():
            unique = tuple(sorted(set(members)))
            if len(unique) < 2:
                dropped["singletons"] += 1
                continue
            groups.add(unique)
    ordered = sorted(groups, key=lambda group: (-len(group), group))
    kept: list[tuple[str, ...]] = []
    for group in ordered:
        if any(set(group) < set(other) for other in kept):
            dropped["subsets"] += 1
            continue
        kept.append(group)
    return [list(group) for group in sorted(kept)], dropped


def _filter_analogies(raw: Any, by_id: Mapping[str, Mapping[str, Any]]
                      ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    dropped = {
        "malformed": 0,
        "unknown_ids": 0,
        "self_links": 0,
        "cross_anchor": 0,
        "duplicates": 0,
    }
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(raw, (list, tuple)):
        return out, dropped
    for item in raw:
        if not isinstance(item, Mapping):
            dropped["malformed"] += 1
            continue
        left = str(item.get("source_finding") or item.get("source") or "")
        right = str(item.get("target_finding") or item.get("target") or "")
        if not left or not right or left == right:
            dropped["self_links"] += 1
            continue
        left_view = by_id.get(left)
        right_view = by_id.get(right)
        if left_view is None or right_view is None:
            dropped["unknown_ids"] += 1
            continue
        if left_view["anchor"]["id"] != right_view["anchor"]["id"]:
            dropped["cross_anchor"] += 1
            continue
        pair = tuple(sorted((left, right)))
        if pair in seen:
            dropped["duplicates"] += 1
            continue
        seen.add(pair)
        order_n = item.get("order_n")
        if not isinstance(order_n, int) or isinstance(order_n, bool):
            order_n = None
        out.append({"pair": pair, "order_n": order_n})
    out.sort(key=lambda entry: entry["pair"])
    return out, dropped


def _rejected_engine_claims(raw: Mapping[str, Any]) -> list[str]:
    rejected: list[str] = []
    if raw.get("truth_assessed") is True:
        rejected.append("truth_assessed")
    if raw.get("verified") is True:
        rejected.append("verified")
    verification = raw.get("verification")
    if isinstance(verification, Mapping) and verification.get("state") == "verified":
        rejected.append("verification")
    if raw.get("support_edges_created") is True:
        rejected.append("support_edges_created")
    if raw.get("support_edges"):
        rejected.append("support_edges")
    return rejected


# ---------------------------------------------------------------------------
# Engine calls
# ---------------------------------------------------------------------------


def _default_search(rows: Sequence[Mapping[str, Any]], *, root: str,
                    ocelot_root: str | Path | None, mode: str, timeout: int
                    ) -> dict[str, Any]:
    try:
        from ozagia.core import ocelot_code as OC
        result = OC.cluster_findings(
            rows, root=root, ocelot_root=ocelot_root, mode=mode,
            timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - absence/error is a state
        return {
            "state": "unavailable",
            "available": False,
            "actual_mode": None,
            "engine_id": None,
            "engine_version": None,
            "engine_source": None,
            "clusters": [],
            "analogies": [],
            "input_hash_state": "not_checked",
            "error_detail": _safe_detail(
                f"{type(exc).__name__}: {exc}", paths=(root, ocelot_root)),
            "error_code": "engine_failed",
            "rejected_engine_claims": [],
            "raw": {},
        }
    state = str(result.get("state") or "unavailable")
    meta = result.get("ocelot")
    meta = meta if isinstance(meta, Mapping) else {}
    if state == "ready" and result.get("available") is False:
        state = "unavailable"
    if state not in {"ready", "not_installed", "unavailable"}:
        state = "unavailable"
    return {
        "state": state,
        "available": state == "ready",
        "actual_mode": result.get("actual_mode"),
        "engine_id": (_engine_token(result.get("engine"), 64)
                      if state == "ready" else None),
        "engine_version": (_engine_token(meta.get("version"), MAX_VERSION)
                           if state == "ready" else None),
        "engine_source": (_engine_token(meta.get("source"), 32)
                          if state == "ready" else None),
        "clusters": result.get("ocelot_clusters") or [],
        "analogies": result.get("analogies") or [],
        "input_hash_state": "not_reported",
        "error_detail": _safe_detail(result.get("error"),
                                     paths=(root, ocelot_root)),
        "error_code": ("engine_failed" if state == "unavailable"
                       else "not_installed" if state == "not_installed" else None),
        "rejected_engine_claims": [],
        "raw": result,
    }


def _injected_search(engine: Callable[[Mapping[str, Any]], Any],
                     payload: Mapping[str, Any], *, input_hash: str,
                     root: str, ocelot_root: str | Path | None
                     ) -> dict[str, Any]:
    try:
        raw = engine(payload)
    except Exception as exc:  # noqa: BLE001 - absence/error is a state
        return {
            "state": "unavailable",
            "available": False,
            "actual_mode": "injected",
            "engine_id": None,
            "engine_version": None,
            "engine_source": None,
            "clusters": [],
            "analogies": [],
            "input_hash_state": "not_checked",
            "error_detail": _safe_detail(
                f"{type(exc).__name__}: {exc}", paths=(root, ocelot_root)),
            "error_code": "engine_failed",
            "rejected_engine_claims": [],
            "raw": {},
        }
    if not isinstance(raw, Mapping):
        return {
            "state": "invalid",
            "available": False,
            "actual_mode": "injected",
            "engine_id": None,
            "engine_version": None,
            "engine_source": None,
            "clusters": [],
            "analogies": [],
            "input_hash_state": "not_checked",
            "error_detail": "the injected engine returned no mapping",
            "error_code": "invalid_engine_output",
            "rejected_engine_claims": [],
            "raw": {},
        }

    state = raw.get("state")
    if state is None:
        state = "ready" if raw.get("available") is not False else "unavailable"
    state = str(state)
    if state == "error":
        state = "unavailable"
    if state == "no_findings":
        state = "not_installed"
    if state not in {"ready", "not_installed", "unavailable", "invalid"}:
        state = "invalid"
    if state == "ready" and raw.get("available") is False:
        state = "unavailable"

    reported_hash = _normalised_hash(raw.get("input_hash") or raw.get("input_sha256"))
    hash_mismatch = reported_hash is not None and reported_hash != input_hash
    if hash_mismatch:
        state = "invalid"

    engine_id = _engine_token(raw.get("engine"), 64)
    if state == "ready" and engine_id is None:
        engine_id = "injected"
    engine_version = _engine_token(
        raw.get("engine_version") or raw.get("version"), MAX_VERSION)
    return {
        "state": state,
        "available": state == "ready",
        "actual_mode": (_engine_token(raw.get("actual_mode"), 32) or "injected"
                        if state == "ready" else None),
        "engine_id": engine_id,
        "engine_version": engine_version,
        "engine_source": "injected",
        "clusters": raw.get("clusters") if raw.get("clusters") is not None
        else raw.get("groups") or [],
        "analogies": raw.get("analogies") or [],
        "input_hash_state": ("not_reported" if reported_hash is None
                             else "mismatch" if hash_mismatch else "match"),
        "error_detail": (
            "the engine reported an input hash that does not match this request"
            if hash_mismatch else None
        ),
        "error_code": (
            "engine_input_hash_mismatch" if hash_mismatch
            else "invalid_engine_state" if state == "invalid"
            else "engine_unavailable" if state == "unavailable" else None
        ),
        "rejected_engine_claims": _rejected_engine_claims(raw),
        "raw": dict(raw),
    }


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------


def _engine_view(engine_id: str | None, version: str | None, source: str | None,
                 *, expected_id: str | None, expected_version: str | None,
                 satisfied: bool | None) -> dict[str, Any]:
    return {
        "id": engine_id,
        "version": version,
        "version_state": ("resolved" if version
                          else "unknown" if engine_id else "not_applicable"),
        "source": source,
        "id_supported": (engine_id == ENGINE_ID if engine_id else None),
        "constraint": {
            "expected_id": expected_id,
            "expected_version": expected_version,
            "satisfied": satisfied,
        },
    }


def _verifier_view() -> dict[str, Any]:
    return {
        "state": "not_verified",
        "method": None,
        "reason": (
            "no verifier ran; Ocelot structural analogies are candidates, not "
            "proofs, support or truth"
        ),
        "truth_assessed": False,
    }


def _base_section(mode: str) -> dict[str, Any]:
    return {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "requested": True,
        "available": False,
        "state": "invalid",
        "reason_code": "invalid_request",
        "reason": "the adapter request was not evaluated",
        "mode": mode,
        "actual_mode": None,
        "operation": {
            "id": OPERATION_ID,
            "version": OPERATION_VERSION,
            "read_only": True,
            "description": (
                "candidate structural analogies among claims with the same "
                "epistemic class and exact cited-source identity"
            ),
            "worker_protocol": "ocelot_reconcile_worker/findings-v1",
            "input_scope": "claims with at least one cited source",
            "cross_class_links": False,
            "creates_support_edges": False,
        },
        "engine": _engine_view(None, None, None, expected_id=None,
                               expected_version=None, satisfied=None),
        "verifier": _verifier_view(),
        "inputs": {
            "hash_algorithm": "sha256",
            "projection_hash": None,
            "observed_projection_hash": None,
            "hash_state": "not_checked",
            "claim_set_hash": None,
            "payload_hash": None,
            "input_hash": None,
            "selected_keys": [],
            "clustered_keys": [],
            "expected_input_hash": None,
            "expected_input_hash_state": "not_checked",
            "engine_input_hash_state": "not_checked",
        },
        "output": {
            "schema": {"namespace": f"{SCHEMA_NAMESPACE}.output",
                       "version": SCHEMA_VERSION},
            "state": "not_produced",
            "hash": None,
            "hash_algorithm": "sha256",
            "candidate_cluster_count": 0,
            "analogy_count": 0,
            "exact_group_count": 0,
            "engine_reported_truth_assessed": False,
            "rejected_engine_claims": [],
        },
        "candidate_clusters": [],
        "analogies": [],
        "exact_groups": [],
        "coverage": {
            "claims_total": 0,
            "claims_valid": 0,
            "claims_selected": 0,
            "claims_anchored": 0,
            "claims_without_sources": 0,
            "claims_omitted_by_limit": 0,
            "claims_malformed": 0,
            "anchors": 0,
            "exact_groups": 0,
            "candidate_clusters": 0,
            "analogies": 0,
            "claims_in_exact_groups": 0,
            "claims_in_candidate_clusters": 0,
            "claims_in_analogies": 0,
            "source_identity_kinds": {},
            "unresolved_source_identities": 0,
        },
        "score": {
            "state": "unavailable",
            "value": None,
            "source": None,
            "calibrated": False,
            "reason": (
                "the Ocelot worker returns structural clusters and analogies, "
                "not a calibrated score"
            ),
        },
        "truth_assessed": False,
        "support_edges_created": False,
        "epistemic_boundary": dict(_EPISTEMIC_BOUNDARY),
        "omissions": [],
        "truncated": _truncation(0),
        "unknown": list(_UNKNOWN),
        "deferred": list(_DEFERRED),
    }


def _output_body(section: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": {"namespace": f"{SCHEMA_NAMESPACE}.output",
                   "version": SCHEMA_VERSION},
        "input_hash": section["inputs"].get("input_hash"),
        "candidate_clusters": section["candidate_clusters"],
        "analogies": section["analogies"],
        "exact_groups": section["exact_groups"],
        "coverage": section["coverage"],
        "score": section["score"],
    }


def _fill_output(section: dict[str, Any], *, state: str,
                 dropped: int = 0, reason: str = "bytes") -> None:
    section["coverage"].update({
        "exact_groups": len(section["exact_groups"]),
        "candidate_clusters": len(section["candidate_clusters"]),
        "analogies": len(section["analogies"]),
        "claims_in_exact_groups": len({
            claim["key"] for group in section["exact_groups"]
            for claim in group["claims"]}),
        "claims_in_candidate_clusters": len({
            claim["key"] for group in section["candidate_clusters"]
            for claim in group["claims"]}),
        "claims_in_analogies": len({
            value for analogy in section["analogies"]
            for value in (analogy["source"], analogy["target"])}),
    })
    if dropped:
        section["omissions"] = sorted(set(
            list(section["omissions"])
            + [f"{dropped} adapter output row(s) omitted by the {reason} limit"]))
        section["truncated"] = _truncation(
            int(section["truncated"].get("omitted") or 0) + dropped,
            reason=reason)
    body = _output_body(section)
    output_hash = (_hash(body) if state in {"produced", "fallback_only"}
                   else None)
    section["output"].update({
        "state": state,
        "hash": output_hash,
        "candidate_cluster_count": len(section["candidate_clusters"]),
        "analogy_count": len(section["analogies"]),
        "exact_group_count": len(section["exact_groups"]),
    })


def _enforce_bytes(section: dict[str, Any]) -> None:
    dropped = 0

    def too_big() -> bool:
        return len(C.canonical_json(section).encode("utf-8")) > MAX_SECTION_BYTES

    if not too_big():
        return
    for key in ("analogies", "candidate_clusters", "exact_groups"):
        values = section[key]
        while values and too_big():
            values.pop()
            dropped += 1
    if dropped:
        _fill_output(section, state=section["output"]["state"], dropped=dropped)


def _set_invalid(section: dict[str, Any], code: str, reason: str) -> None:
    section.update({
        "available": False,
        "state": "invalid",
        "reason_code": code,
        "reason": _line(reason),
    })
    section["candidate_clusters"] = []
    section["analogies"] = []
    section["exact_groups"] = []
    section["output"].update({
        "state": "not_produced",
        "hash": None,
        "candidate_cluster_count": 0,
        "analogy_count": 0,
        "exact_group_count": 0,
    })


def _set_absent(section: dict[str, Any], *, state: str, code: str,
                reason: str, detail: str | None = None) -> None:
    section.update({
        "available": False,
        "state": state,
        "reason_code": code,
        "reason": _line(reason),
    })
    if detail:
        section["omissions"] = sorted(set(
            list(section["omissions"]) + [f"engine detail: {_line(detail)}"]))


def _build_section(projection: Mapping[str, Any], *, engine: Any = None,
                   ocelot_root: str | Path | None = None, mode: str = "auto",
                   timeout: int = 120, root: str | Path | None = None,
                   expected_engine_id: str | None = None,
                   expected_version: str | None = None,
                   expected_input_hash: str | None = None,
                   max_claims: int = MAX_CLAIMS) -> dict[str, Any]:
    injected = engine is not None
    section = _base_section("injected" if injected else str(mode))

    if not injected and mode not in MODES:
        _set_invalid(section, "unsupported_mode",
                     f"mode must be one of {MODES}")
        return section
    if injected and not callable(engine):
        _set_invalid(section, "invalid_engine", "engine must be callable")
        return section
    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError):
        timeout_value = 0
    if timeout_value <= 0:
        _set_invalid(section, "invalid_timeout",
                     "timeout must be a positive integer")
        return section
    try:
        claim_limit = int(max_claims)
    except (TypeError, ValueError):
        claim_limit = 0
    if not 1 <= claim_limit <= 500:
        _set_invalid(section, "invalid_limit",
                     "max_claims must be an integer between 1 and 500")
        return section

    if expected_engine_id is not None:
        if (not isinstance(expected_engine_id, str)
                or not expected_engine_id
                or len(expected_engine_id) > MAX_TEXT):
            _set_invalid(section, "invalid_request",
                         "expected_engine_id must be a bounded string")
            return section
    if expected_version is not None:
        if (not isinstance(expected_version, str)
                or not expected_version
                or len(expected_version) > MAX_VERSION):
            _set_invalid(section, "invalid_request",
                         "expected_version must be a bounded string")
            return section
    expected_hash = None
    if expected_input_hash is not None:
        expected_hash = _normalised_hash(expected_input_hash)
        if expected_hash is None:
            _set_invalid(section, "invalid_request",
                         "expected_input_hash must be a 64-hex SHA-256")
            return section
        section["inputs"]["expected_input_hash"] = expected_hash

    declared_hash = projection.get("hash")
    try:
        observed_hash = _hash({key: value for key, value in projection.items()
                               if key != "hash"})
    except Exception:  # noqa: BLE001 - malformed input is an explicit state
        observed_hash = None
    section["inputs"].update({
        "projection_hash": declared_hash if isinstance(declared_hash, str) else None,
        "observed_projection_hash": observed_hash,
    })
    if not isinstance(declared_hash, str) or not declared_hash:
        section["inputs"]["hash_state"] = "missing"
        _set_invalid(section, "projection_hash_missing",
                     "the projection has no hash; the adapter cannot bind its input")
        return section
    if observed_hash != declared_hash:
        section["inputs"]["hash_state"] = "mismatch"
        _set_invalid(section, "projection_hash_mismatch",
                     "the projection content does not match its hash; no engine ran")
        return section
    section["inputs"]["hash_state"] = "match"

    root_value = root if root is not None else projection.get("root")
    if not isinstance(root_value, (str, Path)) or not str(root_value):
        _set_invalid(section, "invalid_root",
                     "the projection root is missing or is not a path string")
        return section
    root_text = _text(root_value, 512)
    if ocelot_root is not None and not isinstance(ocelot_root, (str, Path)):
        _set_invalid(section, "invalid_request",
                     "ocelot_root must be a path string")
        return section

    sources = _source_index(projection)
    raw_claims = projection.get("claims")
    claims: list[dict[str, Any]] = []
    malformed = 0
    if isinstance(raw_claims, (list, tuple)):
        for raw in raw_claims:
            if not isinstance(raw, Mapping):
                malformed += 1
                continue
            view = _claim_view(raw)
            if view is None:
                malformed += 1
                continue
            claims.append(view)
    claims.sort(key=lambda view: view["key"])
    omitted_by_limit = max(0, len(claims) - claim_limit)
    selected = claims[:claim_limit]
    without_sources = [view for view in selected if not view["source_ids"]]
    anchored = [view for view in selected if view["source_ids"]]

    coverage = section["coverage"]
    coverage.update({
        "claims_total": len(raw_claims) if isinstance(raw_claims, (list, tuple)) else 0,
        "claims_valid": len(claims),
        "claims_selected": len(selected),
        "claims_anchored": len(anchored),
        "claims_without_sources": len(without_sources),
        "claims_omitted_by_limit": omitted_by_limit,
        "claims_malformed": malformed,
    })
    if malformed:
        section["omissions"].append(
            f"{malformed} malformed claim row(s) ignored")
    if omitted_by_limit:
        section["omissions"].append(
            f"{omitted_by_limit} claim(s) omitted by the claim limit")
        section["truncated"] = _truncation(omitted_by_limit, reason="limit")
    for view in without_sources[:8]:
        section["omissions"].append(
            f"claim {view['key']} omitted: no cited source anchor")
    if len(without_sources) > 8:
        section["omissions"].append(
            f"{len(without_sources) - 8} more claim(s) omitted: no cited source")

    section["inputs"]["selected_keys"] = [view["key"] for view in selected]
    section["inputs"]["clustered_keys"] = [view["key"] for view in anchored]

    payload_rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    anchors: dict[str, dict[str, Any]] = {}
    identity_kinds: dict[str, int] = {}
    unresolved = 0
    unresolved_claims: list[str] = []
    for number, view in enumerate(anchored, start=1):
        anchor = _anchor_for(view, sources)
        if anchor is None:
            continue
        view["anchor"] = anchor
        anchors.setdefault(anchor["id"], anchor)
        for kind in anchor["source_identity_kinds"]:
            identity_kinds[kind] = identity_kinds.get(kind, 0) + 1
        if anchor["unresolved_sources"]:
            unresolved += 1
            unresolved_claims.append(view["key"])
        payload_id = f"c{number:04d}"
        row = _payload_row(number, view, anchor)
        payload_rows.append(row)
        by_id[payload_id] = view
    coverage.update({
        "anchors": len(anchors),
        "source_identity_kinds": dict(sorted(identity_kinds.items())),
        "unresolved_source_identities": unresolved,
    })
    if unresolved_claims:
        section["omissions"].append(
            f"{len(unresolved_claims)} claim(s) have an unresolved source identity")
        for key in unresolved_claims[:8]:
            section["omissions"].append(
                f"claim {key} source identity is unresolved; a session-local "
                "fallback was used")
        if len(unresolved_claims) > 8:
            section["omissions"].append(
                f"{len(unresolved_claims) - 8} more claim(s) have an unresolved "
                "source identity")

    claim_set_body = {
        "schema": {"namespace": f"{SCHEMA_NAMESPACE}.claim_set",
                   "version": SCHEMA_VERSION},
        "projection_hash": declared_hash,
        "claims": [_claim_fingerprint(view) for view in selected],
    }
    payload_body = {
        "schema": {"namespace": f"{SCHEMA_NAMESPACE}.request",
                   "version": SCHEMA_VERSION},
        "findings": payload_rows,
    }
    claim_set_hash = _hash(claim_set_body)
    payload_hash = _hash(payload_body)
    input_hash = _hash({
        "projection_hash": declared_hash,
        "claim_set_hash": claim_set_hash,
        "payload_hash": payload_hash,
    })
    section["inputs"].update({
        "claim_set_hash": claim_set_hash,
        "payload_hash": payload_hash,
        "input_hash": input_hash,
    })
    if expected_hash is not None:
        if expected_hash != input_hash:
            section["inputs"]["expected_input_hash_state"] = "mismatch"
            _set_invalid(section, "input_hash_mismatch",
                         "the input hash does not match the selected projection "
                         "claims and payload; no engine ran")
            return section
        section["inputs"]["expected_input_hash_state"] = "match"

    def _exact_groups_now() -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for view in anchored:
            grouped.setdefault(view["anchor"]["id"], []).append(view)
        groups: list[dict[str, Any]] = []
        for anchor_id in sorted(grouped):
            views = grouped[anchor_id]
            if len(views) < 2:
                continue
            groups.append(_exact_group(len(groups) + 1, anchors[anchor_id], views))
        return groups

    all_exact_groups = _exact_groups_now()
    exact_groups = all_exact_groups[:MAX_EXACT_GROUPS]
    exact_omitted = len(all_exact_groups) - len(exact_groups)
    if exact_omitted:
        section["omissions"].append(
            f"{exact_omitted} exact group(s) omitted by the output limit")
        section["truncated"] = _truncation(
            int(section["truncated"].get("omitted") or 0) + exact_omitted,
            reason="limit")
    section["exact_groups"] = exact_groups

    if not anchored:
        section.update({
            "available": False,
            "state": "no_claims",
            "reason_code": "no_claims",
            "reason": (
                "no selected claim cites a source; there is no source anchor "
                "to compare"
            ),
        })
        _fill_output(section, state="no_output")
        return section

    if injected:
        search = _injected_search(
            engine, {
                "schema": REQUEST_SCHEMA,
                "input_hash": input_hash,
                "findings": payload_rows,
            }, input_hash=input_hash, root=root_text,
            ocelot_root=ocelot_root)
    else:
        search = _default_search(
            payload_rows, root=root_text, ocelot_root=ocelot_root,
            mode=mode, timeout=timeout_value)
    section["actual_mode"] = search.get("actual_mode")
    section["inputs"]["engine_input_hash_state"] = str(
        search.get("input_hash_state") or "not_checked")
    section["engine"] = _engine_view(
        search.get("engine_id"), search.get("engine_version"),
        search.get("engine_source"), expected_id=expected_engine_id,
        expected_version=expected_version, satisfied=None)
    section["output"]["rejected_engine_claims"] = list(
        search.get("rejected_engine_claims") or [])
    section["output"]["engine_reported_truth_assessed"] = (
        "truth_assessed" in section["output"]["rejected_engine_claims"])

    if search["state"] != "ready":
        state = "invalid" if search["state"] == "invalid" else search["state"]
        detail = search.get("error_detail")
        code = search.get("error_code") or state
        if state == "not_installed":
            reason = (
                "Ocelot is not installed or resolvable; no engine ran and no "
                "candidate clusters were produced"
            )
        elif state == "invalid":
            reason = detail or "the engine result was invalid; no output was accepted"
        else:
            reason = (
                "Ocelot is unavailable or the engine failed; no candidate "
                "clusters were accepted"
            )
        if state == "invalid":
            _set_invalid(section, str(code), reason)
        else:
            _set_absent(section, state=state, code=str(code), reason=reason,
                        detail=detail)
            _fill_output(section, state="fallback_only")
            _enforce_bytes(section)
        return section

    problems: list[tuple[str, str]] = []
    if expected_engine_id is not None and search.get("engine_id") != expected_engine_id:
        problems.append((
            "engine_mismatch",
            "the engine id does not match the pinned id"))
    if expected_version is not None:
        version = search.get("engine_version")
        if version is None:
            problems.append((
                "version_mismatch",
                "the engine version is unknown; the pinned version cannot be "
                "confirmed"))
        elif version != expected_version:
            problems.append((
                "version_mismatch",
                "the engine version does not match the pinned version"))
    section["engine"]["constraint"]["satisfied"] = (
        False if problems else True if (
            expected_engine_id is not None or expected_version is not None
        ) else None)
    if problems:
        _set_invalid(section, problems[0][0], problems[0][1])
        return section

    groups, group_drops = _filter_groups(search.get("clusters"), by_id)
    analogies, analogy_drops = _filter_analogies(search.get("analogies"), by_id)
    candidate_clusters: list[dict[str, Any]] = []
    for group in groups[:MAX_CANDIDATE_CLUSTERS]:
        views = [by_id[payload_id] for payload_id in group]
        anchor = anchors[views[0]["anchor"]["id"]]
        count = sum(
            1 for analogy in analogies
            if analogy["pair"][0] in group and analogy["pair"][1] in group)
        candidate_clusters.append(
            _candidate_cluster(len(candidate_clusters) + 1, anchor, views, count))
    omitted_clusters = max(0, len(groups) - len(candidate_clusters))
    analogy_views = [
        _analogy_view(by_id[left], by_id[right],
                      anchors[by_id[left]["anchor"]["id"]], order_n)
        for left, right, order_n in (
            (entry["pair"][0], entry["pair"][1], entry["order_n"])
            for entry in analogies[:MAX_ANALOGIES])
    ]
    omitted_analogies = max(0, len(analogies) - len(analogy_views))

    section.update({
        "available": True,
        "state": "ready",
        "reason_code": None,
        "reason": (
            "Ocelot completed the structural comparison; no truth, support or "
            "derivation was assessed"
        ),
        "candidate_clusters": candidate_clusters,
        "analogies": analogy_views,
    })
    if omitted_clusters:
        section["omissions"].append(
            f"{omitted_clusters} candidate cluster(s) omitted by the output limit")
    if omitted_analogies:
        section["omissions"].append(
            f"{omitted_analogies} analogies omitted by the output limit")
    limit_omitted = (omitted_by_limit + exact_omitted
                     + omitted_clusters + omitted_analogies)
    if limit_omitted:
        section["truncated"] = _truncation(limit_omitted, reason="limit")
    for label in ("unknown_ids", "malformed", "self_links", "cross_anchor",
                  "duplicates"):
        count = group_drops.get(label, 0) + analogy_drops.get(label, 0)
        if count:
            section["omissions"].append(
                f"{count} engine {label.replace('_', ' ')} row(s) ignored")
    if group_drops.get("subsets"):
        section["omissions"].append(
            f"{group_drops['subsets']} subset cluster(s) ignored")
    if group_drops.get("split_clusters"):
        section["omissions"].append(
            f"{group_drops['split_clusters']} cross-anchor cluster(s) split")
    _fill_output(section, state="produced")
    _enforce_bytes(section)
    return section


def attach_ocelot(projection: Mapping[str, Any], *, engine: Any = None,
                  ocelot_root: str | Path | None = None, mode: str = "auto",
                  timeout: int = 120, root: str | Path | None = None,
                  expected_engine_id: str | None = None,
                  expected_version: str | None = None,
                  expected_input_hash: str | None = None,
                  max_claims: int = MAX_CLAIMS) -> dict[str, Any]:
    """Return a copy of ``projection`` with an ``ocelot`` candidate section.

    The input projection is never mutated.  With ``engine`` omitted, the
    existing optional Ocelot path is resolved through
    ``ozagia.core.ocelot_code.cluster_findings``; absence is reported, not
    raised.  An injected engine must accept the request mapping and return a
    worker-shaped mapping (``state``, ``clusters``, ``analogies``, optional
    ``engine``/``engine_version``/``input_hash``).

    A valid, non-invalid attach result re-seals the joined document in its
    ``hash`` field.  An ``invalid`` result leaves the base ``hash`` unchanged:
    it must not be read as a re-sealed projection.
    """
    if not isinstance(projection, Mapping):
        raise OcelotProjectionError("projection must be a mapping")
    out = copy.deepcopy(dict(projection))
    section = _build_section(
        out, engine=engine, ocelot_root=ocelot_root, mode=mode,
        timeout=timeout, root=root, expected_engine_id=expected_engine_id,
        expected_version=expected_version,
        expected_input_hash=expected_input_hash, max_claims=max_claims)
    out["ocelot"] = section
    if section["state"] != "invalid":
        out["hash"] = _hash({key: value for key, value in out.items()
                             if key != "hash"})
    return out


__all__ = [
    "ENGINE_ID",
    "MAX_ANALOGIES",
    "MAX_CANDIDATE_CLUSTERS",
    "MAX_CLAIMS",
    "MAX_EXACT_GROUPS",
    "MAX_SECTION_BYTES",
    "MODES",
    "OcelotProjectionError",
    "OPERATION_ID",
    "OPERATION_VERSION",
    "REQUEST_SCHEMA",
    "SCHEMA_NAMESPACE",
    "SCHEMA_VERSION",
    "attach_ocelot",
]
