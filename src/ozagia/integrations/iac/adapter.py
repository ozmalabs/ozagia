"""Opt-in source-only IaC adapter.

Enable with ``[adapters] modules = ["ozagia.integrations.iac.adapter:register"]``.
This is static partial analysis, never an account, cluster, or runtime view.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
from pathlib import PurePosixPath
from typing import Any

from ozagia.core.infra_discovery import iter_candidate_paths, read_source

MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
# Parsed per-file graphs are immutable and keyed by root, path, dialect and
# content digest. The cache never stores source text or extracted values, and
# covers the adapter's 5,000-file discovery budget with headroom.
PARSE_CACHE_MAX = 6000
_PARSE_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


def _cached_parse(key: tuple[Any, ...], parse: Any) -> Any:
    found = _PARSE_CACHE.get(key)
    if found is not None:
        _PARSE_CACHE.move_to_end(key)
        return found
    result = parse()
    _PARSE_CACHE[key] = result
    while len(_PARSE_CACHE) > PARSE_CACHE_MAX:
        _PARSE_CACHE.popitem(last=False)
    return result


def clear_parse_cache() -> None:
    """Drop cached per-file graphs (for benchmarks and process hygiene)."""
    _PARSE_CACHE.clear()


def _safe_identifier(value: Any) -> str:
    """Bound graph labels to identifiers and paths, not arbitrary source text."""
    import re
    if not isinstance(value, str) or len(value) > 256:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9_./:@#-]+", value) else ""


_TERRAFORM_ADDRESS_PREFIX = {
    "module": "module", "variable": "var", "local": "local",
    "output": "output", "provider": "provider",
}


def _address_alias(family: str, kind: str, name: str) -> str:
    """Terraform's full address for a block whose stored name is a bare label."""
    prefix = _TERRAFORM_ADDRESS_PREFIX.get(kind) if family == "terraform" else None
    if not prefix:
        return ""
    alias = f"{prefix}.{name}"
    return alias if alias != name else ""


def index(ctx: Any) -> None:
    from ozagia.core.arm_graph import index_arm_text
    from ozagia.core.cloudformation_graph import (
        CloudFormationParseError, parse_cloudformation_source,
    )
    from ozagia.core.deployment_graph import extract_deployment_metadata
    from ozagia.core.gitops_graph import GitOpsParseError, parse_gitops_source
    from ozagia.core.kubernetes_graph import (
        KubernetesParseError, merge_kubernetes_graphs, parse_kubernetes_source,
    )
    from ozagia.core.pulumi_graph import parse_pulumi_yaml
    from ozagia.core.terraform_graph import index_terraform_text, merge_terraform_graphs

    all_nodes: list[tuple[Any, str]] = []
    all_edges: list[Any] = []
    kube_graphs = []
    terraform_graphs = []
    diagnostics: list[tuple[str, str]] = []
    manifests: dict[str, str] = {}
    terraform_by_dir: dict[str, dict[str, str]] = {}
    # One bounded walk: register every candidate even when a source cannot be
    # read or the cumulative content budget is reached, otherwise every refresh
    # would repeatedly treat those paths as newly discovered.
    total_bytes = 0
    budget_exceeded = False
    root_key = str(ctx.root.resolve())
    for rel, kind in iter_candidate_paths(ctx.root):
        if ctx.skipped(rel):
            continue
        ctx.file_node(rel, indexed=False)
        if budget_exceeded:
            continue
        text = read_source(ctx.root, rel)
        if text is None:
            diagnostics.append((rel, "Infrastructure source unreadable or over the per-file limit"))
            continue
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            diagnostics.append((rel, "Infrastructure source budget exceeded; later files skipped"))
            budget_exceeded = True
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if kind == "terraform":
            scope = PurePosixPath(rel).parent.as_posix()
            dialect = ("opentofu" if rel.lower().endswith((".tofu", ".tofu.json"))
                       else "terraform")
            terraform_by_dir.setdefault(scope, {})[dialect] = rel
            graph = _cached_parse(
                (root_key, rel, "terraform", scope, digest),
                lambda: index_terraform_text(rel, text, module_scope=scope))
            terraform_graphs.append(graph)
            diagnostics.extend(
                (rel, f"Terraform: {message}") for _path, message in graph.errors)
        elif kind == "arm":
            arm = index_arm_text(rel, text)
            all_nodes.extend((node, "arm") for node in arm.nodes)
            all_edges.extend(arm.edges)
            diagnostics.extend(
                (rel, f"ARM/Bicep: {message}") for _path, message in arm.errors)
        else:
            manifests[rel] = text
            if PurePosixPath(rel).name == "Pulumi.yaml":
                pulumi = parse_pulumi_yaml(text, rel)
                all_nodes.extend((node, "pulumi") for node in pulumi.nodes)
                all_edges.extend(pulumi.edges)
                if pulumi.diagnostics or pulumi.unresolved:
                    diagnostics.append((rel, "Pulumi declarations contain unsupported or unresolved expressions"))
            if rel.endswith(".json") and '"resources"' in text and '"contentVersion"' in text:
                arm = index_arm_text(rel, text)
                all_nodes.extend((node, "arm") for node in arm.nodes)
                all_edges.extend(arm.edges)
                diagnostics.extend(
                    (rel, f"ARM/Bicep: {message}")
                    for _path, message in arm.errors)
            if "argoproj.io" in text or "toolkit.fluxcd.io" in text:
                try:
                    gitops = parse_gitops_source(text, rel)
                except GitOpsParseError:
                    pass
                else:
                    all_nodes.extend((node, "gitops") for node in gitops.nodes)
                    all_edges.extend(gitops.edges)
                    if gitops.unresolved:
                        diagnostics.append((rel, f"GitOps references unresolved: {len(gitops.unresolved)}"))
            if "AWSTemplateFormatVersion" in text or "Resources" in text:
                try:
                    cloudformation = parse_cloudformation_source(text, rel)
                except CloudFormationParseError:
                    pass
                else:
                    all_nodes.extend((node, "cloudformation") for node in cloudformation.nodes)
                    all_edges.extend(cloudformation.edges)
                    if cloudformation.unresolved:
                        diagnostics.append((rel, f"CloudFormation references unresolved: {len(cloudformation.unresolved)}"))
            try:
                graph = _cached_parse(
                    (root_key, rel, "kubernetes", digest),
                    lambda: parse_kubernetes_source(text, rel))
            except KubernetesParseError as exc:
                if "apiVersion" in text and "kind" in text:
                    diagnostics.append(
                        (rel, f"Kubernetes: {exc}"))
                continue
            if graph.nodes:
                kube_graphs.append(graph)
    for _scope, dialects in sorted(terraform_by_dir.items()):
        if len(dialects) > 1:
            path = dialects.get("terraform") or next(iter(dialects.values()))
            diagnostics.append((
                path,
                "both Terraform and OpenTofu sources in one module scope; "
                "documented dialect precedence is not evaluated",
            ))
    if terraform_graphs:
        terraform = merge_terraform_graphs(terraform_graphs)
        all_nodes.extend((node, "terraform") for node in terraform.nodes)
        all_edges.extend(terraform.edges)
        by_key = {node.key: node.path for node in terraform.nodes}
        for edge in terraform.unresolved:
            diagnostics.append((by_key.get(edge.source, ""), "Terraform reference unresolved"))
    if kube_graphs:
        kube = merge_kubernetes_graphs(kube_graphs)
        all_nodes.extend((node, "kubernetes") for node in kube.nodes)
        all_edges.extend(kube.edges)
        by_key = {node.key: node.path for node in kube.nodes}
        for ref in kube.unresolved:
            diagnostics.append((by_key.get(ref.source, ""), "Kubernetes reference unresolved"))
    deployment = extract_deployment_metadata(manifests)
    diagnostics.extend((rel, "unsupported or invalid Helm/Kustomize source")
                       for rel, _ in deployment.diagnostics)
    all_nodes.extend((node, "deployment") for node in deployment.nodes)
    all_edges.extend(deployment.edges)
    ids: dict[str, int] = {}
    for node, family in all_nodes:
        name = _safe_identifier(getattr(node, "address", None) or getattr(node, "name", None)
                                or getattr(node, "logical_id", None)
                                or getattr(node, "logical_name", ""))
        path = _safe_identifier(getattr(node, "path", ""))
        kind = _safe_identifier(getattr(node, "kind", None) or
                                ("resource" if family == "pulumi" else ""))
        if not name or not path or not kind:
            continue
        attrs: dict[str, Any] = {"name": name, "address": name, "path": path,
                                 "kind": kind, "family": family,
                                 "provenance": "declared",
                                 "confidence": getattr(node, "confidence", "partial"),
                                 "source_digest": node.source_digest,
                                 "extractor_version": getattr(node, "extractor_version", "gitops-source-v1")}
        line = getattr(node, "start_line", None)
        pointer = getattr(node, "pointer", None)
        if isinstance(line, int):
            attrs["line"] = line
        if isinstance(pointer, str) and _safe_identifier(pointer):
            attrs["pointer"] = pointer
        alias = _safe_identifier(_address_alias(family, kind, name))
        if alias:
            attrs["aliases"] = alias
        instance_mode = _safe_identifier(getattr(node, "instance_mode", None))
        if instance_mode:
            attrs["instance_mode"] = instance_mode
        for crd_field in ("crd_group", "crd_kind", "crd_scope"):
            crd_value = _safe_identifier(getattr(node, crd_field, None))
            if crd_value:
                attrs[crd_field] = crd_value
        namespace = _safe_identifier(getattr(node, "namespace", None))
        if namespace:
            attrs["namespace"] = namespace
        raw_key = node.key
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        nid = ctx.builder.node("InfraResource", attrs, key=("infra", digest))
        ids[raw_key] = nid
        ctx.builder.edge(nid, "declared_in", ctx.file_node(path))
    for edge in all_edges:
        if edge.source in ids and edge.target in ids:
            relation = _safe_identifier(edge.relation)
            if relation:
                edge_attrs: dict[str, Any] = {"provenance": edge.provenance}
                evidence = _safe_identifier(getattr(edge, "evidence", None))
                if evidence:
                    edge_attrs["evidence"] = evidence
                scope = _safe_identifier(getattr(edge, "scope", None))
                if scope:
                    edge_attrs["scope"] = scope
                ctx.builder.edge(ids[edge.source], "infra_" + relation,
                                 ids[edge.target], edge_attrs)
    for path, message in diagnostics[:100]:
        if not _safe_identifier(path):
            continue
        key = hashlib.sha256((path + "\0" + message).encode()).hexdigest()
        ctx.builder.node("InfraDiagnostic", {"path": path, "message": message},
                         key=("infra-diagnostic", key))


def _context(memory: Any, **kwargs: Any) -> dict[str, Any]:
    from ozagia.core.infra_query import context
    freshness = memory.staleness()
    if freshness.get("freshness") != "fresh":
        return {"context": "", "keys": [], "results": [], "edges": [],
                "freshness": freshness.get("freshness", "unknown"),
                "reason": f"snapshot freshness is {freshness.get('freshness', 'unknown')}"}
    raw_history = kwargs.get("history") or []
    if not isinstance(raw_history, (list, tuple)):
        raw_history = []
    history = [item[:500] for item in raw_history[:4] if isinstance(item, str)]
    result = context(
        memory, str(kwargs.get("query", "")), history=history,
        max_chars=min(4000, max(0, int(kwargs.get("max_chars", 1600)))),
        limit=min(50, max(0, int(kwargs.get("limit", 8)))))
    result["freshness"] = "fresh"
    return result


def register(registry: Any) -> None:
    from dataclasses import asdict

    from ozagia.core.iac_findings import import_sarif
    from ozagia.core.infra_query import cache_stats, locate, traverse

    def query(memory: Any, operation: str, **args: Any) -> dict[str, Any]:
        freshness = memory.staleness().get("freshness", "unknown")
        if freshness != "fresh":
            return {"freshness": freshness, "complete": False, "results": []}
        if operation == "locate":
            result = locate(memory, str(args.get("query", "")),
                            limit=int(args.get("limit", 20)))
        else:
            result = traverse(memory, str(args.get("key", "")), direction=operation,
                              depth=int(args.get("depth", 2)),
                              limit=int(args.get("limit", 30)))
        result["freshness"] = freshness
        return result

    registry.add_indexer(index)
    registry.add_graph_query("infra.context", _context)
    def status(memory: Any, **_args: Any) -> dict[str, Any]:
        type_index = getattr(memory, "nodes_of_type", None)
        if callable(type_index):
            resources = [(memory.nodes.get(nid) or {}).get("attrs", {})
                         for nid in type_index("InfraResource")]
            diagnostics = [(memory.nodes.get(nid) or {}).get("attrs", {})
                           for nid in type_index("InfraDiagnostic")]
        else:
            resources = [n.get("attrs", {}) for n in memory.nodes.values()
                         if n.get("type") == "InfraResource"]
            diagnostics = [n.get("attrs", {}) for n in memory.nodes.values()
                           if n.get("type") == "InfraDiagnostic"]
        families: dict[str, int] = {}
        extractors: set[str] = set()
        for attrs in resources:
            family = str(attrs.get("family") or "unknown")
            families[family] = families.get(family, 0) + 1
            version = attrs.get("extractor_version")
            if isinstance(version, str):
                extractors.add(version)
        out = getattr(memory, "_out", {}) or {}
        edges = sum(1 for bucket in out.values() for edge in bucket
                    if str(edge.get("type", "")).startswith("infra_"))
        return {"freshness": memory.staleness().get("freshness", "unknown"),
                "resources": len(resources), "families": dict(sorted(families.items())),
                "edges": edges, "extractors": sorted(extractors),
                "query_cache": cache_stats(memory),
                "diagnostics": diagnostics[:100],
                "complete": False,
                "note": "Static, partial source analysis; not deployed state."}

    registry.add_graph_query("infra.status", status)
    def parse_findings(_memory: Any, args: dict[str, Any]) -> dict[str, Any]:
        imported = import_sarif(str(args.get("sarif", "")))
        return {"findings": [asdict(finding) for finding in imported.findings],
                "diagnostics": list(imported.diagnostics)}

    registry.add_mcp_tool({
        "name": "infra_import_sarif",
        "description": "Parse a supplied SARIF report into bounded, source-attributed metadata; no scan is run or saved.",
        "inputSchema": {"type": "object", "properties": {"sarif": {"type": "string"}},
                        "required": ["sarif"], "additionalProperties": False},
    }, parse_findings)
    def parse_cdk(_memory: Any, args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.cdk_graph import CdkAssemblyError, read_cdk_assembly
        manifest = args.get("manifest")
        templates = args.get("templates") or {}
        if not isinstance(manifest, str) or not isinstance(templates, dict):
            return {"ok": False, "error": "manifest must be a string and templates an object"}
        try:
            graph = read_cdk_assembly(manifest, templates=templates)
        except (CdkAssemblyError, ValueError):
            return {"ok": False,
                    "error": "unsupported or invalid supplied CDK assembly"}
        return {"ok": True, **graph.to_dict()}

    registry.add_mcp_tool({
        "name": "infra_import_cdk",
        "description": ("Read a supplied CDK cloud assembly manifest and optional "
                        "templates; never synthesizes, runs or loads assets."),
        "inputSchema": {"type": "object", "properties": {
            "manifest": {"type": "string"},
            "templates": {"type": "object",
                          "additionalProperties": {"type": "string"}}},
            "required": ["manifest"], "additionalProperties": False},
    }, parse_cdk)
    registry.add_mcp_tool({
        "name": "infra_status", "description": "Report infrastructure extraction limitations.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    }, lambda memory, _args: status(memory))
    registry.add_mcp_tool({
        "name": "infra_context",
        "description": "Find source-attributed, partial infrastructure structure; not live state.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "history": {"type": "array", "items": {"type": "string"},
                        "maxItems": 4, "default": []},
            "max_chars": {"type": "integer", "default": 1600},
            "limit": {"type": "integer", "default": 8}},
            "required": ["query"], "additionalProperties": False},
    }, lambda memory, args: _context(memory, **args))
    for operation in ("locate", "dependencies", "impact"):
        def handler(memory: Any, args: dict[str, Any], op: str = operation) -> dict[str, Any]:
            return query(memory, op, **args)

        registry.add_graph_query("infra." + operation,
                                 lambda memory, op=operation, **args: query(memory, op, **args))
        prop = ({"query": {"type": "string"}} if operation == "locate" else
                {"key": {"type": "string"}, "depth": {"type": "integer", "default": 2}})
        prop["limit"] = {"type": "integer", "default": 20}
        registry.add_mcp_tool({
            "name": "infra_" + operation,
            "description": "Query partial, source-only infrastructure structure; not live state.",
            "inputSchema": {"type": "object", "properties": prop,
                            "required": ["query" if operation == "locate" else "key"],
                            "additionalProperties": False},
        }, handler)
