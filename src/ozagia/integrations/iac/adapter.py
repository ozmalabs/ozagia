"""Opt-in source-only IaC adapter.

Enable with ``[adapters] modules = ["ozagia.integrations.iac.adapter:register"]``.
This is static partial analysis, never an account, cluster, or runtime view.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import fnmatch
import hashlib
import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from ozagia.config import load_project_config
from ozagia.core.infra_discovery import (
    is_skipped_path,
    iter_candidate_paths,
    read_source,
    skipped_summary,
)

MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
# Parsed per-file graphs are immutable and keyed by root, path, dialect and
# content digest. The cache never stores source text or arbitrary values;
# Terraform graphs retain only allowlisted literal backend identity fields.
# It covers the adapter's 5,000-file discovery budget with headroom.
PARSE_CACHE_MAX = 6000
_PARSE_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


@dataclass(frozen=True)
class _AdapterEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str
    confidence: str = "high"


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
    if not isinstance(value, str) or len(value) > 256:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9_./:@#-]+", value) else ""


def _safe_diagnostic_path(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256:
        return ""
    return (value if re.fullmatch(
        r"[A-Za-z0-9_./:@#*?+\[\]-]+", value) else "")


def _safe_evidence(value: Any) -> str:
    """Keep bounded static evidence labels, including spaces, not source text."""
    if (not isinstance(value, str) or len(value) > 256
            or any(ord(char) < 32 for char in value)
            or not value.strip()):
        return ""
    return value


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
    from ozagia.core.deployment_graph import (
        extract_deployment_metadata, helm_template_paths,
    )
    from ozagia.core.gitops_graph import GitOpsParseError, parse_gitops_source
    from ozagia.core.kubernetes_graph import (
        KubernetesParseError, merge_kubernetes_graphs, parse_kubernetes_source,
    )
    from ozagia.core.pulumi_graph import parse_pulumi_yaml
    from ozagia.core.terraform_graph import (
        index_terraform_text,
        merge_terraform_graphs,
        resolve_terraform_roots,
    )

    config = load_project_config(ctx.root)
    all_nodes: list[tuple[Any, str]] = []
    all_edges: list[Any] = []
    kube_graphs = []
    terraform_graphs = []
    diagnostics: list[tuple[str, str]] = []
    manifests: dict[str, str] = {}
    terraform_by_dir: dict[str, dict[str, str]] = {}
    terraform_paths: list[str] = []
    ops_sources: dict[str, str] = {}
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
        if kind == "ops":
            ops_sources[rel] = text
        elif kind == "terraform":
            terraform_paths.append(rel)
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
        lock_dirs: set[str] = set()
        for path in terraform_paths:
            current = PurePosixPath(path).parent
            while True:
                rel_lock = (PurePosixPath(".terraform.lock.hcl")
                            if current.as_posix() == "."
                            else current / ".terraform.lock.hcl")
                lock_path = ctx.root / rel_lock.as_posix()
                if lock_path.is_file() and not lock_path.is_symlink():
                    lock_dirs.add(current.as_posix())
                if current.as_posix() == ".":
                    break
                current = current.parent
        terraform = resolve_terraform_roots(
            terraform, terraform_paths, lock_dirs=tuple(lock_dirs),
            explicit_roots=config.iac_roots,
            remote_states=config.remote_states,
        )
        all_nodes.extend((node, "terraform") for node in terraform.nodes)
        all_edges.extend(terraform.edges)
        by_key = {node.key: node.path for node in terraform.nodes}
        for edge in terraform.unresolved:
            diagnostics.append((
                by_key.get(edge.source, ""),
                f"Terraform {edge.relation} unresolved: {edge.evidence}",
            ))

    template_paths = helm_template_paths(manifests)
    for rel, text in sorted(manifests.items()):
        if rel in template_paths:
            diagnostics.append((rel, "Helm template source; not parsed or rendered"))
            continue
        try:
            graph = _cached_parse(
                (root_key, rel, "kubernetes",
                 hashlib.sha256(text.encode("utf-8")).hexdigest()),
                lambda: parse_kubernetes_source(text, rel))
        except KubernetesParseError as exc:
            if "apiVersion" in text and "kind" in text:
                diagnostics.append((rel, f"Kubernetes: {exc}"))
            continue
        if graph.nodes:
            kube_graphs.append(graph)
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

    extra_attrs: dict[str, dict[str, Any]] = {}
    environment_by_node: dict[str, str | None] = {}
    ambiguous_environment_nodes: set[str] = set()

    def add_config_node(family: str, kind: str, name: str, path: str,
                        extra: dict[str, Any]) -> Any:
        key = f"infra-config:v1:{family}:{hashlib.sha256(name.encode()).hexdigest()}"
        node = SimpleNamespace(
            key=key, kind=kind, name=name, address=name, path=path,
            source_digest=hashlib.sha256(
                (family + "\0" + name + "\0" + repr(extra)).encode()).hexdigest(),
            extractor_version="iac-config-v1", confidence="high",
        )
        extra_attrs[key] = extra
        all_nodes.append((node, family))
        return node

    def path_matches(path: str, prefix: str) -> bool:
        prefix = prefix.rstrip("/") or "."
        return prefix == "." or path == prefix or path.startswith(prefix + "/")

    def environment_matches(path: str, namespace: str | None) -> set[str]:
        matched = {
            environment.name for environment in config.environments
            if any(path_matches(path, prefix) for prefix in environment.paths)
            or (namespace is not None and namespace in environment.namespaces)
        }
        return matched

    def existing_project_path(rel: str, *, directory: bool) -> Path | None:
        if is_skipped_path(ctx.root, rel):
            return None
        current = ctx.root
        for part in PurePosixPath(rel).parts:
            current = current / part
            if current.is_symlink():
                return None
        if directory:
            return current if current.is_dir() else None
        return current if current.is_file() else None

    for environment in config.environments:
        values = tuple(dict.fromkeys((
            environment.name, *environment.paths, *environment.namespaces,
            *(value for value in (
                environment.account_alias, environment.region,
                environment.cluster, environment.risk,
            ) if value),
            *environment.pipelines,
        )))
        add_config_node(
            "environment", "Environment", environment.name, ".",
            {"environment": environment.name,
             "environment_values": list(values),
             "paths": list(environment.paths),
             "namespaces": list(environment.namespaces),
             "account_alias": environment.account_alias,
             "region": environment.region, "cluster": environment.cluster,
             "risk": environment.risk, "pipelines": list(environment.pipelines)},
        )

    source_nodes = list(all_nodes)
    for service in config.services:
        service_path = "."
        if service.paths:
            existing_dir = existing_project_path(service.paths[0], directory=True)
            if existing_dir is not None:
                service_path = service.paths[0]
            else:
                diagnostics.append((
                    service.paths[0], "Service path directory is missing or skipped",
                ))
        service_extra = {
            "service": service.name, "chart": service.chart,
            "values_globs": list(service.values_globs),
            "buildspec": service.buildspec, "pipeline": service.pipeline,
            "namespace": service.namespace,
            "environment": service.environment, "paths": list(service.paths),
        }
        service_node = add_config_node(
            "service", "Service", service.name, service_path, service_extra)
        if service.environment:
            environment_by_node[service_node.key] = service.environment
        linked: list[str] = []

        def link(target: Any, relation: str, evidence: str = "project config") -> None:
            all_edges.append(_AdapterEdge(
                service_node.key, target.key, relation, "declared",
                evidence, service_path, "high",
            ))
            linked.append(target.key)
            if service.environment:
                previous = environment_by_node.get(target.key)
                if previous is None:
                    if target.key not in ambiguous_environment_nodes:
                        environment_by_node[target.key] = service.environment
                elif previous != service.environment:
                    environment_by_node[target.key] = None
                    ambiguous_environment_nodes.add(target.key)
                    diagnostics.append((
                        getattr(target, "path", service_path),
                        "Service mappings assign conflicting environments; facet omitted",
                    ))

        deployment_nodes = [
            node for node, family in source_nodes if family == "deployment"
        ]
        if service.chart:
            chart_matches = {
                node.key: node for node in deployment_nodes
                if getattr(node, "kind", "") == "helm_chart"
                and (PurePosixPath(node.path).parent.as_posix() == service.chart
                     or node.name == service.chart)
            }
            if len(chart_matches) == 1:
                link(next(iter(chart_matches.values())), "service_chart")
            else:
                diagnostics.append((
                    service.chart,
                    "Service chart target " + (
                        "is missing" if not chart_matches else "is ambiguous"),
                ))

        for pattern in service.values_globs:
            matches = {
                node.key: node for node in deployment_nodes
                if getattr(node, "kind", "") == "helm_values_file"
                and fnmatch.fnmatchcase(node.path, pattern)
            }
            if not matches:
                diagnostics.append((pattern, "Service values glob matched no source files"))
            for target in matches.values():
                link(target, "service_values")

        for prefix in service.paths:
            candidates = [
                node for node, _family in source_nodes
                if path_matches(str(getattr(node, "path", "")), prefix)
                and str(getattr(node, "kind", "")).lower() in {
                    "deployment", "statefulset", "daemonset", "replicaset",
                    "job", "pod", "serviceaccount", "ingress",
                    "kustomize_overlay",
                }
                and (service.namespace is None
                     or getattr(node, "namespace", None) == service.namespace)
            ]
            grouped: dict[tuple[str, str, str], list[Any]] = {}
            for target in candidates:
                identity = (str(getattr(target, "kind", "")),
                            str(getattr(target, "name", "")),
                            str(getattr(target, "namespace", "") or ""))
                grouped.setdefault(identity, []).append(target)
            for identity, group in grouped.items():
                if len(group) != 1:
                    diagnostics.append((
                        prefix, f"Service {identity[0]} target is ambiguous",
                    ))
                    continue
                target = group[0]
                kind = str(getattr(target, "kind", "")).lower()
                relation = {
                    "deployment": "service_deployment",
                    "statefulset": "service_deployment",
                    "daemonset": "service_deployment",
                    "replicaset": "service_deployment",
                    "job": "service_deployment",
                    "pod": "service_deployment",
                    "serviceaccount": "service_account",
                    "ingress": "service_ingress",
                    "kustomize_overlay": "service_kustomize",
                }.get(kind)
                if relation:
                    link(target, relation)
            if not candidates:
                diagnostics.append((prefix, "Service path has no matching deployment sources"))

        def add_source_file(path: str, kind: str) -> None:
            if existing_project_path(path, directory=False) is None:
                message = ("is skipped" if is_skipped_path(ctx.root, path)
                           else "is missing or not a regular source file")
                diagnostics.append((path, f"Service {kind} target {message}"))
                return
            key = f"infra-config:v1:service-source:{hashlib.sha256((kind + chr(0) + path).encode()).hexdigest()}"
            source = SimpleNamespace(
                key=key, kind=kind, name=PurePosixPath(path).name,
                address=PurePosixPath(path).name, path=path,
                source_digest=hashlib.sha256(path.encode()).hexdigest(),
                extractor_version="iac-config-v1", confidence="high",
            )
            extra_attrs[key] = {"service": service.name}
            if key not in {node.key for node, _family in all_nodes}:
                all_nodes.append((source, "service"))
            link(source, "service_" + kind)

        if service.buildspec:
            add_source_file(service.buildspec, "buildspec")
        if service.pipeline:
            add_source_file(service.pipeline, "pipeline")
    for node, _family in source_nodes:
        node_path = str(getattr(node, "path", ""))
        namespace = getattr(node, "namespace", None)
        matches = environment_matches(
            node_path, namespace if isinstance(namespace, str) else None)
        if len(matches) > 1:
            ambiguous_environment_nodes.add(node.key)
            environment_by_node[node.key] = None
            diagnostics.append((node_path, "Environment facet is ambiguous"))
        elif len(matches) == 1 and node.key not in ambiguous_environment_nodes:
            configured_environment = next(iter(matches))
            existing = environment_by_node.get(node.key)
            if existing is None:
                environment_by_node[node.key] = configured_environment
            elif existing != configured_environment:
                environment_by_node[node.key] = None
                ambiguous_environment_nodes.add(node.key)

    from ozagia.integrations.iac.ops import emit_ops
    diagnostics.extend(emit_ops(ctx, ops_sources))

    skip_info = skipped_summary(ctx.root)
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
        extras = extra_attrs.get(raw_key, {})
        for field in (
                "service", "chart", "buildspec", "pipeline", "account_alias",
                "region", "cluster", "risk", "environment"):
            value = extras.get(field)
            safe = _safe_identifier(value)
            if safe:
                attrs[field] = safe
        for field in (
                "environment_values", "paths", "namespaces", "pipelines",
                "values_globs"):
            value = extras.get(field)
            if isinstance(value, (list, tuple)):
                safe_values = [
                    safe for item in value
                    if (safe := _safe_identifier(item))
                ]
                if safe_values:
                    attrs[field] = safe_values
        facet = environment_by_node.get(raw_key)
        if facet is not None:
            safe_facet = _safe_identifier(facet)
            if safe_facet:
                attrs["environment"] = safe_facet
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        nid = ctx.builder.node("InfraResource", attrs, key=("infra", digest))
        ids[raw_key] = nid
        if kind not in {"root", "Environment", "Service"}:
            ctx.builder.edge(nid, "declared_in", ctx.file_node(path))
    for edge in all_edges:
        if edge.source in ids and edge.target in ids:
            relation = _safe_identifier(edge.relation)
            if relation:
                edge_attrs: dict[str, Any] = {"provenance": edge.provenance}
                evidence = _safe_evidence(getattr(edge, "evidence", None))
                if evidence:
                    edge_attrs["evidence"] = evidence
                scope = _safe_identifier(getattr(edge, "scope", None))
                if scope:
                    edge_attrs["scope"] = scope
                confidence = _safe_identifier(getattr(edge, "confidence", None))
                if confidence:
                    edge_attrs["confidence"] = confidence
                ctx.builder.edge(ids[edge.source], "infra_" + relation,
                                 ids[edge.target], edge_attrs)
    skip_key = hashlib.sha256(
        (root_key + "\0iac-skip-summary").encode()).hexdigest()
    ctx.builder.node(
        "InfraDiagnostic",
        {"path": ".", "message": "IaC skipped paths",
         "category": "skipped", "count": int(skip_info["count"]),
         "paths": list(skip_info["paths"]),
         "truncated": bool(skip_info["truncated"])},
        key=("infra-skip-summary", skip_key),
    )
    for path, message in diagnostics[:100]:
        safe_path = _safe_diagnostic_path(path)
        if not safe_path:
            continue
        key = hashlib.sha256((path + "\0" + message).encode()).hexdigest()
        ctx.builder.node("InfraDiagnostic", {"path": safe_path, "message": message},
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

    # Reads IaC sources from disk (reingest runs it for IaC files and the
    # files it used); no indexed source, test or doc text.
    registry.add_indexer(index, texts=False)
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
        skip_reports = [item for item in diagnostics
                        if item.get("category") == "skipped"]
        skipped_count = max(
            (int(item.get("count") or 0) for item in skip_reports), default=0)
        skipped_paths = list(dict.fromkeys(
            path for item in skip_reports
            for path in item.get("paths", []) if isinstance(path, str)
        ))[:500]
        return {"freshness": memory.staleness().get("freshness", "unknown"),
                "resources": len(resources), "families": dict(sorted(families.items())),
                "edges": edges, "extractors": sorted(extractors),
                "query_cache": cache_stats(memory),
                "diagnostics": diagnostics[:100],
                "skipped": {"count": skipped_count, "paths": skipped_paths,
                            "truncated": any(bool(item.get("truncated"))
                                             for item in skip_reports)},
                "complete": False,
                "note": "Static, partial source analysis; not deployed state."}

    registry.add_graph_query("infra.status", status)
    from ozagia.integrations.iac.ops import register_ops
    register_ops(registry)
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
