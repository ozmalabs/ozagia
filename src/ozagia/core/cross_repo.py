"""Conservative static checks over explicitly linked repository snapshots.

This module reports evidence present in snapshots; it does not infer links or
verify runtime contracts and deployments.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ozagia.core import dependency_links
from ozagia.core import federation


_CODE_TYPES = {"Symbol", "File", "ApiCodeReference"}
_API_COVERAGE = {"produces_api", "consumes_api", "tests_api"}


def _member_nodes(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot = item.get("snapshot") or {}
    return {f"{item['alias']}::{node.get('key')}": node
            for node in snapshot.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("key"), str)}


def _attrs(node: dict[str, Any] | None) -> dict[str, Any]:
    value = node.get("attrs") if isinstance(node, dict) else None
    return value if isinstance(value, dict) else {}


def check(root: str | Path | None = None,
          roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Check explicitly declared cross-repository API and code/IaC links.

    Supported relations are ``produces_api``, ``consumes_api`` and
    ``tests_api`` (code node -> ApiOperation), ``same_api_operation``
    (ApiOperation -> ApiOperation, comparing method and path), and
    ``deployed_by`` (code node -> InfraResource, comparing an explicit
    ``service`` value when both endpoints provide one). Unknown relations and
    unsupported endpoint types are reported as unknown.

    The report also includes ``dependency_links``, a read-only comparison of
    declared package dependencies between members; it is built from current
    member manifests and does not affect the API ``overall`` verdict.
    """
    selected = federation._root(root)
    if roots is not None:
        roots = tuple(roots)
    # Federation use updates explicitly designated references by default;
    # callers can opt out through without_reference_sync().
    members = federation._members(selected, roots)
    link_report = federation._declared_links(selected, members)
    reference_report = federation.reference_status(selected)
    references: dict[Path, dict[str, Any]] = {}
    for reference in reference_report.get("references", []):
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            continue
        try:
            references[Path(reference["path"]).expanduser().resolve()] = reference
        except (OSError, RuntimeError, ValueError):
            continue
    nodes: dict[str, dict[str, Any]] = {}
    member_states: dict[str, str] = {}
    public_members = []
    member_errors = []
    for item in members:
        nodes.update(_member_nodes(item))
        state, detail = federation._freshness(item)
        reference = references.get(item["root"])
        sync = reference.get("sync") if reference else None
        sync_details = sync if isinstance(sync, dict) else {}
        sync_state = reference.get("state") if reference else None
        member_states[item["alias"]] = state
        public_member = {
            "id": item["alias"], "name": item["label"],
            "root": str(item["root"]), "snapshot": str(item["snapshot_path"]),
            "state": state, "detail": detail,
            "head": item["attrs"].get("head"),
            "generated_at": item["attrs"].get("generated_at"),
            "nodes": len((item["snapshot"] or {}).get("nodes", [])),
            "edges": len((item["snapshot"] or {}).get("edges", [])),
        }
        if reference:
            public_member.update({"reference": True, "pin": reference.get("pin"),
                                  "sync_state": sync_state, "sync": sync})
        public_members.append(public_member)
        if state != "fresh":
            member_errors.append({"id": item["alias"], "root": str(item["root"]),
                                 "state": state, "detail": detail or item["error"]})
        if reference:
            if sync_state == "error":
                member_states[item["alias"]] = "sync_error"
                member_errors.append({"id": item["alias"], "root": str(item["root"]),
                                      "state": "sync_error",
                                      "detail": sync_details.get("reason", "reference sync failed")})
            elif sync_state == "pending":
                member_states[item["alias"]] = "sync_pending"
                member_errors.append({"id": item["alias"], "root": str(item["root"]),
                                      "state": "sync_pending",
                                      "detail": "reference update is still running"})
            elif sync_state == "updated" or (isinstance(sync, dict)
                                               and sync.get("index_state") == "stale_until_reindexed"):
                member_states[item["alias"]] = "index_stale"
                member_errors.append({"id": item["alias"], "root": str(item["root"]),
                                      "state": "index_stale",
                                      "detail": "reference snapshot needs reindexing"})
            elif sync_state not in {"current", "pinned"}:
                member_states[item["alias"]] = "sync_unknown"
                member_errors.append({"id": item["alias"], "root": str(item["root"]),
                                      "state": "sync_unknown",
                                      "detail": "reference sync state is not verified"})

    reference_errors = reference_report.get("errors", [])
    if reference_errors:
        member_errors.append({"state": "reference_manifest_error",
                              "detail": "reference manifest has invalid entries"})

    dependency_report = dependency_links.analyze(members, member_states)

    checks: list[dict[str, Any]] = []
    for link in link_report["links"]:
        if link["type"] == "configured_by":
            continue  # Evaluated by the dedicated current-source checker below.
        source_key, target_key = link["source"], link["target"]
        source, target = nodes.get(source_key), nodes.get(target_key)
        source_type = source.get("type") if source else None
        target_type = target.get("type") if target else None
        result = "unknown"
        reason = "unsupported_relation"
        comparison: dict[str, Any] = {}
        comparison_scope = "none"
        relation = link["type"]
        if relation in _API_COVERAGE:
            if source_type not in _CODE_TYPES or target_type != "ApiOperation":
                reason = "unsupported_endpoint_types"
            else:
                result, reason = "declared", "explicit_api_coverage_link"
                comparison_scope = "explicit_link_presence_only"
        elif relation == "same_api_operation":
            if source_type != "ApiOperation" or target_type != "ApiOperation":
                reason = "unsupported_endpoint_types"
            else:
                a = _attrs(source)
                b = _attrs(target)
                fields = ("method", "path")
                if not all(isinstance(a.get(f), str) and isinstance(b.get(f), str)
                           and a.get(f) and b.get(f) for f in fields):
                    reason = "operation_identity_missing"
                else:
                    comparison_scope = "method_and_path_equality"
                    comparison = {field: {"source": a[field], "target": b[field]}
                                  for field in fields}
                    result = "pass" if all(a[f] == b[f] for f in fields) else "fail"
                    reason = "operation_identity_matches" if result == "pass" else "operation_identity_mismatch"
        elif relation == "deployed_by":
            if source_type not in _CODE_TYPES or target_type != "InfraResource":
                reason = "unsupported_endpoint_types"
            else:
                a, b = _attrs(source), _attrs(target)
                left, right = a.get("service"), b.get("service")
                if isinstance(left, str) and left and isinstance(right, str) and right:
                    comparison_scope = "service_attribute_equality"
                    comparison = {"service": {"source": left, "target": right}}
                    result = "pass" if left == right else "fail"
                    reason = "service_identity_matches" if result == "pass" else "service_identity_mismatch"
                else:
                    reason = "service_identity_unavailable"

        # A stale, missing, invalid, or unverifiable snapshot cannot support a
        # positive result, even when the stored link and attrs line up.
        aliases = (source_key.partition("::")[0], target_key.partition("::")[0])
        states = [member_states.get(alias, "missing") for alias in aliases]
        evaluated_result = result
        if any(state != "fresh" for state in states):
            result, reason = "unknown", "member_snapshot_not_verified_fresh"
        checks.append({"source": source_key, "target": target_key,
                       "relation": relation, "source_type": source_type,
                       "target_type": target_type, "result": result,
                       "evaluated_result": evaluated_result,
                       "reason": reason, "comparison": comparison,
                       "comparison_scope": comparison_scope,
                       "provenance": link.get("provenance"),
                       "source_manifest": link.get("source_manifest"),
                       "member_states": states})

    errors = list(dict.fromkeys(link_report["errors"] + link_report.get("member_errors", [])))
    for message in errors:
        checks.append({"result": "unknown", "reason": "invalid_declared_link",
                       "detail": message, "provenance": None})
    counts = {state: sum(c["result"] == state for c in checks)
              for state in ("pass", "fail", "declared", "unknown")}
    from ozagia.core import cross_config

    configuration = cross_config.check(selected, roots) if any(
        link["type"] == "configured_by" for link in link_report["links"]
    ) else None
    if any(error["state"] in {"missing", "invalid"} for error in member_errors):
        overall = "incomplete"
    elif member_errors or counts["unknown"] or (configuration and configuration["status"] == "unknown"):
        overall = "unknown"
    elif not checks and not configuration:
        overall = "no_checks"
    elif configuration and any(item["status"] == "observed_missing"
                               for item in configuration["results"]):
        overall = "findings"
    elif counts["fail"]:
        overall = "fail"
    else:
        overall = "complete"
    return {"ok": True, "root": str(selected), "checks": checks,
            "counts": counts, "link_state": link_report["state"],
            "members": public_members, "member_errors": member_errors,
            "overall": overall, "errors": errors,
            "reference_errors": len(reference_errors),
            "configuration": configuration,
            "dependency_links": dependency_report,
            "note": "Static evidence only; API compatibility, test execution, and deployment state are not verified."}


__all__ = ["check"]
