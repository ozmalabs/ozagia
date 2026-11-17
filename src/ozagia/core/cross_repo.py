"""Conservative static checks over explicitly linked repository snapshots.

This module reports evidence present in snapshots; it does not infer links or
verify runtime contracts and deployments.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ozagia.core import federation


_CODE_TYPES = {"Symbol", "File", "ApiCodeReference"}
_API_COVERAGE = {"produces_api", "consumes_api", "tests_api"}


def _member_nodes(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot = item.get("snapshot") or {}
    return {f"{item['alias']}::{node.get('key')}": node
            for node in snapshot.get("nodes", [])
            if isinstance(node, dict) and isinstance(node.get("key"), str)}


def check(root: str | Path | None = None,
          roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Check explicitly declared cross-repository API and code/IaC links.

    Supported relations are ``produces_api``, ``consumes_api`` and
    ``tests_api`` (code node -> ApiOperation), ``same_api_operation``
    (ApiOperation -> ApiOperation, comparing method and path), and
    ``deployed_by`` (code node -> InfraResource, comparing an explicit
    ``service`` value when both endpoints provide one). Unknown relations and
    unsupported endpoint types are reported as unknown.
    """
    selected = federation._root(root)
    members = federation._members(selected, roots)
    link_report = federation._declared_links(selected, members)
    nodes: dict[str, dict[str, Any]] = {}
    member_by_alias = {item["alias"]: item for item in members}
    for item in members:
        nodes.update(_member_nodes(item))

    checks: list[dict[str, Any]] = []
    for link in link_report["links"]:
        source_key, target_key = link["source"], link["target"]
        source, target = nodes.get(source_key), nodes.get(target_key)
        source_type = source.get("type") if source else None
        target_type = target.get("type") if target else None
        result = "unknown"
        reason = "unsupported_relation"
        comparison: dict[str, Any] = {}
        relation = link["type"]
        if relation in _API_COVERAGE:
            if source_type not in _CODE_TYPES or target_type != "ApiOperation":
                reason = "unsupported_endpoint_types"
            else:
                result, reason = "declared", "explicit_api_coverage_link"
        elif relation == "same_api_operation":
            if source_type != "ApiOperation" or target_type != "ApiOperation":
                reason = "unsupported_endpoint_types"
            else:
                a = (source.get("attrs") or {})
                b = (target.get("attrs") or {})
                fields = ("method", "path")
                if not all(isinstance(a.get(f), str) and isinstance(b.get(f), str)
                           and a.get(f) and b.get(f) for f in fields):
                    reason = "operation_identity_missing"
                else:
                    comparison = {field: {"source": a[field], "target": b[field]}
                                  for field in fields}
                    result = "pass" if all(a[f] == b[f] for f in fields) else "fail"
                    reason = "operation_identity_matches" if result == "pass" else "operation_identity_mismatch"
        elif relation == "deployed_by":
            if source_type not in _CODE_TYPES or target_type != "InfraResource":
                reason = "unsupported_endpoint_types"
            else:
                a, b = source.get("attrs") or {}, target.get("attrs") or {}
                left, right = a.get("service"), b.get("service")
                if isinstance(left, str) and left and isinstance(right, str) and right:
                    comparison = {"service": {"source": left, "target": right}}
                    result = "pass" if left == right else "fail"
                    reason = "service_identity_matches" if result == "pass" else "service_identity_mismatch"
                else:
                    reason = "service_identity_unavailable"

        # A stale, missing, invalid, or unverifiable snapshot cannot support a
        # positive result, even when the stored link and attrs line up.
        aliases = (source_key.partition("::")[0], target_key.partition("::")[0])
        states = [member_by_alias.get(alias, {}).get("error") or
                  federation._freshness(member_by_alias[alias])[0]
                  if alias in member_by_alias else "missing" for alias in aliases]
        evaluated_result = result
        if any(state != "fresh" for state in states):
            result, reason = "unknown", "member_snapshot_not_verified_fresh"
        checks.append({"source": source_key, "target": target_key,
                       "relation": relation, "source_type": source_type,
                       "target_type": target_type, "result": result,
                       "evaluated_result": evaluated_result,
                       "reason": reason, "comparison": comparison,
                       "provenance": link.get("provenance"),
                       "source_manifest": link.get("source_manifest"),
                       "member_states": states})

    errors = list(link_report["errors"])
    for message in errors:
        checks.append({"result": "unknown", "reason": "invalid_declared_link",
                       "detail": message, "provenance": None})
    counts = {state: sum(c["result"] == state for c in checks)
              for state in ("pass", "fail", "declared", "unknown")}
    return {"ok": True, "root": str(selected), "checks": checks,
            "counts": counts, "link_state": link_report["state"],
            "errors": errors,
            "note": "Static snapshot evidence only; API compatibility, test execution, and deployment state are not verified."}


__all__ = ["check"]
