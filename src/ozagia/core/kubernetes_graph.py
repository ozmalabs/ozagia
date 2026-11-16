"""Bounded, source-only Kubernetes manifest graph extraction.

This module intentionally stores only Kubernetes identity and a small set of
structural relationships. It never retains arbitrary manifest values.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

EXTRACTOR_VERSION = "kubernetes-source-v2"
MAX_SOURCE_BYTES = 2_000_000
MAX_DOCUMENTS = 256
_CLUSTER_SCOPED = frozenset({
    "ClusterRole", "ClusterRoleBinding", "Namespace", "Node", "PersistentVolume",
    "StorageClass", "CustomResourceDefinition",
})


@dataclass(frozen=True)
class InfraNode:
    key: str
    kind: str
    name: str
    namespace: str | None
    api_version: str
    path: str
    pointer: str
    source_digest: str
    provenance: str = "declared"
    extractor_version: str = EXTRACTOR_VERSION
    pod_labels: tuple[tuple[str, str], ...] = ()
    service_selector: tuple[tuple[str, str], ...] = ()
    references: tuple[tuple[str, str, str, str, str | None], ...] = ()


@dataclass(frozen=True)
class InfraEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class UnresolvedReference:
    source: str
    relation: str
    target_name: str
    namespace: str | None
    evidence: str


@dataclass(frozen=True)
class KubernetesGraph:
    nodes: tuple[InfraNode, ...]
    edges: tuple[InfraEdge, ...]
    unresolved: tuple[UnresolvedReference, ...]
    source_digest: str
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-safe payload suitable for shared graph builders."""
        return {
            "source_path": self.source_path or (self.nodes[0].path if self.nodes else None),
            "source_digest": self.source_digest,
            "extractor_version": EXTRACTOR_VERSION,
            "nodes": [{
                "key": node.key, "kind": node.kind, "name": node.name,
                "namespace": node.namespace, "api_version": node.api_version,
                "path": node.path, "pointer": node.pointer,
                "source_digest": node.source_digest, "provenance": node.provenance,
                "extractor_version": node.extractor_version,
            } for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
            "unresolved": [asdict(ref) for ref in self.unresolved],
            "diagnostics": [],
        }


class KubernetesParseError(ValueError):
    pass


def _documents(source: str, suffix: str, max_documents: int) -> list[Any]:
    if suffix.lower().endswith(".json"):
        try:
            value = json.loads(source)
        except (ValueError, RecursionError) as exc:
            raise KubernetesParseError("invalid JSON manifest") from exc
        if isinstance(value, list):
            docs = value
        else:
            docs = [value]
    else:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KubernetesParseError("YAML parser unavailable; install the openapi-yaml extra") from exc

        class BoundedLoader(yaml.SafeLoader):
            def __init__(self, stream: str):
                super().__init__(stream)

            def compose_node(self, parent: Any, index: Any) -> Any:
                if self.check_event(yaml.AliasEvent):
                    raise KubernetesParseError("YAML aliases are unsupported")
                return super().compose_node(parent, index)

            def construct_mapping(self, node: Any, deep: bool = False) -> Any:
                if isinstance(node, yaml.MappingNode):
                    seen: set[Any] = set()
                    for key_node, _value_node in node.value:
                        key = self.construct_object(key_node, deep=deep)
                        if key in seen:
                            raise KubernetesParseError("duplicate YAML mapping key")
                        seen.add(key)
                return super().construct_mapping(node, deep=deep)

        try:
            docs = list(yaml.load_all(source, Loader=BoundedLoader))
        except KubernetesParseError:
            raise
        except Exception as exc:
            raise KubernetesParseError("invalid or unsupported YAML manifest") from exc
    docs = [doc for doc in docs if doc is not None]
    if len(docs) > max_documents:
        raise KubernetesParseError("manifest document limit exceeded")
    if not all(isinstance(doc, dict) for doc in docs):
        raise KubernetesParseError("manifest document must be a mapping")
    return docs


def parse_kubernetes_source(
    source: str,
    path: str,
    *,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_documents: int = MAX_DOCUMENTS,
) -> KubernetesGraph:
    """Extract namespaced Kubernetes declarations and conservative references.

    Supported relationships: workload selector -> Service (inferred),
    workload -> ServiceAccount, workload -> ConfigMap/Secret names, and
    Ingress -> Service. JSON and multi-document YAML are supported.
    """
    raw = source.encode("utf-8")
    if len(raw) > max_source_bytes:
        raise KubernetesParseError("manifest source size limit exceeded")
    path = PurePosixPath(path).as_posix()
    digest = hashlib.sha256(raw).hexdigest()
    docs = _documents(source, path, max_documents)
    nodes: list[InfraNode] = []
    identities: dict[tuple[str, str | None, str, str], InfraNode] = {}
    for i, doc in enumerate(docs):
        kind = doc.get("kind")
        api = doc.get("apiVersion")
        meta = doc.get("metadata")
        if not isinstance(kind, str) or not isinstance(api, str) or not isinstance(meta, dict):
            continue
        name, namespace = meta.get("name"), meta.get("namespace")
        if not isinstance(name, str) or not name or (namespace is not None and not isinstance(namespace, str)):
            continue
        if any(len(value) > 256 or any(ord(ch) < 32 for ch in value) for value in (kind, api, name, namespace or "")) or len(path) > 4096:
            continue
        # Cluster-scoped identities do not inherit a namespace. Namespace defaults
        # are not inferred because manifests may be applied to different targets.
        ns = namespace if namespace else None
        ident = (kind, ns, name, api)
        pointer = f"/documents/{i}"
        # Duplicate declarations stay distinct by document index; references to
        # duplicates remain unresolved as ambiguous.
        key = "k8s:v1:" + hashlib.sha256("\0".join((path, kind, ns or "", name, api, str(i))).encode()).hexdigest()
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
        template_meta = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
        if kind == "Pod":
            template_meta = meta
        pod_labels = template_meta.get("labels") if isinstance(template_meta.get("labels"), dict) else {}
        svc_selector = spec.get("selector") if kind == "Service" and isinstance(spec.get("selector"), dict) else {}
        safe_pod_labels = tuple(sorted((k, v) for k, v in pod_labels.items() if isinstance(k, str) and isinstance(v, str)))
        safe_selector = tuple(sorted((k, v) for k, v in svc_selector.items() if isinstance(k, str) and isinstance(v, str)))
        node = InfraNode(key, kind, name, ns, api, path, pointer, digest, pod_labels=safe_pod_labels, service_selector=safe_selector)
        nodes.append(node)
        identities.setdefault(ident, node)

    edges: list[InfraEdge] = []
    unresolved: list[UnresolvedReference] = []
    by_kind_name: dict[tuple[str, str | None, str], list[InfraNode]] = {}
    for n in nodes:
        by_kind_name.setdefault((n.kind, n.namespace, n.name), []).append(n)
    for node in nodes:
        # Locate source document by its pointer; no document content enters output.
        doc = docs[int(node.pointer.rsplit("/", 1)[1])]
        kind = node.kind
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
        pod_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
        if kind == "Pod":
            pod_spec = spec
        refs: list[tuple[str, str, str, str]] = []
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "Pod"}:
            sa = pod_spec.get("serviceAccountName")
            if isinstance(sa, str):
                refs.append(("ServiceAccount", sa, "workload_service_account",
                             "spec.template.spec.serviceAccountName", node.namespace))
            for container in pod_spec.get("containers", []) if isinstance(pod_spec.get("containers"), list) else []:
                if not isinstance(container, dict):
                    continue
                for envfrom in container.get("envFrom", []) if isinstance(container.get("envFrom"), list) else []:
                    if not isinstance(envfrom, dict):
                        continue
                    for refkind in ("configMapRef", "secretRef"):
                        ref = envfrom.get(refkind)
                        if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                            refs.append((
                                "ConfigMap" if refkind == "configMapRef" else "Secret",
                                ref["name"], "workload_config_reference",
                                f"{refkind}.name", node.namespace))
            for volume in pod_spec.get("volumes", []) if isinstance(pod_spec.get("volumes"), list) else []:
                if not isinstance(volume, dict):
                    continue
                for refkind, ref_type in (("configMap", "ConfigMap"), ("secret", "Secret")):
                    ref = volume.get(refkind)
                    if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                        refs.append((ref_type, ref["name"],
                                     "workload_config_reference",
                                     f"volumes.{refkind}.name", node.namespace))
        if kind == "Ingress":
            rules = spec.get("rules", [])
            for rule in rules if isinstance(rules, list) else []:
                http = rule.get("http") if isinstance(rule, dict) else None
                for item in http.get("paths", []) if isinstance(http, dict) and isinstance(http.get("paths"), list) else []:
                    backend = item.get("backend") if isinstance(item, dict) else None
                    service = backend.get("service") if isinstance(backend, dict) else None
                    name = service.get("name") if isinstance(service, dict) else None
                    if isinstance(name, str):
                        refs.append(("Service", name, "ingress_service",
                                     "spec.rules[].http.paths[].backend.service.name",
                                     node.namespace))
        if kind in {"RoleBinding", "ClusterRoleBinding"}:
            role_ref = doc.get("roleRef")
            if isinstance(role_ref, dict):
                target_kind = role_ref.get("kind")
                target_name = role_ref.get("name")
                if isinstance(target_kind, str) and isinstance(target_name, str):
                    target_ns = (None if target_kind in _CLUSTER_SCOPED
                                 else node.namespace)
                    refs.append((target_kind, target_name, "rbac_role_ref",
                                 "spec.roleRef", target_ns))
            subjects = doc.get("subjects")
            for subject in subjects if isinstance(subjects, list) else []:
                if not isinstance(subject, dict):
                    continue
                subject_kind = subject.get("kind")
                subject_name = subject.get("name")
                if not isinstance(subject_kind, str) or not isinstance(subject_name, str):
                    continue
                if subject_kind == "ServiceAccount":
                    subject_ns = subject.get("namespace")
                    refs.append(("ServiceAccount", subject_name, "rbac_subject",
                                 "spec.subjects",
                                 subject_ns if isinstance(subject_ns, str) else None))
                elif subject_kind in {"User", "Group"}:
                    # External identities are not graph nodes and their names
                    # can be personal data; do not retain the value.
                    unresolved.append(UnresolvedReference(
                        node.key, "rbac_subject", "<external>", None,
                        "external subject kind"))
                else:
                    unresolved.append(UnresolvedReference(
                        node.key, "rbac_subject", "<unsupported>", None,
                        "unsupported subject kind"))
        # Retain only allowlisted names/evidence for cross-file resolution.
        object.__setattr__(node, "references", tuple(refs))
        for refkind, name, relation, evidence, target_ns in refs:
            if target_ns is None and refkind not in _CLUSTER_SCOPED:
                unresolved.append(UnresolvedReference(
                    node.key, relation, name, None, "namespace unknown"))
                continue
            matches = by_kind_name.get((refkind, target_ns, name), [])
            if len(matches) == 1:
                scope = (f"namespace:{target_ns}" if target_ns is not None
                         else "cluster")
                edges.append(InfraEdge(node.key, matches[0].key, relation,
                                       "declared", evidence, scope))
            else:
                unresolved.append(UnresolvedReference(
                    node.key, relation, name, target_ns,
                    "missing" if not matches else "ambiguous"))
    return KubernetesGraph(tuple(nodes), tuple(edges), tuple(unresolved), digest, path)


def merge_kubernetes_graphs(graphs: list[KubernetesGraph]) -> KubernetesGraph:
    """Resolve retained safe name references and selectors across input files."""
    nodes = [node for graph in graphs for node in graph.nodes]
    edges = [edge for graph in graphs for edge in graph.edges]
    unresolved = [ref for graph in graphs for ref in graph.unresolved]
    by_kind_name: dict[tuple[str, str | None, str], list[InfraNode]] = {}
    services_by_namespace: dict[str | None, list[InfraNode]] = {}
    for node in nodes:
        by_kind_name.setdefault(
            (node.kind, node.namespace, node.name), []).append(node)
        if node.kind == "Service":
            services_by_namespace.setdefault(node.namespace, []).append(node)
    reference_keys = {
        (node.key, relation, name, target_ns)
        for node in nodes
        for _kind, name, relation, _evidence, target_ns in node.references
    }
    unresolved = [ref for ref in unresolved
                  if (ref.source, ref.relation, ref.target_name,
                      ref.namespace) not in reference_keys]
    for node in nodes:
        for kind, name, relation, evidence, target_ns in node.references:
            if target_ns is None and kind not in _CLUSTER_SCOPED:
                unresolved.append(UnresolvedReference(
                    node.key, relation, name, None, "namespace unknown"))
                continue
            matches = by_kind_name.get((kind, target_ns, name), [])
            if len(matches) == 1:
                scope = (f"namespace:{target_ns}" if target_ns is not None
                         else "cluster")
                edge = InfraEdge(node.key, matches[0].key, relation, "declared",
                                 evidence, scope)
                if edge not in edges:
                    edges.append(edge)
            else:
                unresolved.append(UnresolvedReference(
                    node.key, relation, name, target_ns,
                    "missing" if not matches else "ambiguous"))
        if node.kind not in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "Pod"} or node.namespace is None:
            continue
        labels = dict(node.pod_labels)
        if not labels:
            continue
        matching_services: dict[tuple[str, str], list[InfraNode]] = {}
        for service in services_by_namespace.get(node.namespace, ()):
            selector = dict(service.service_selector)
            if selector and all(labels.get(k) == v for k, v in selector.items()):
                matching_services.setdefault((service.namespace or "", service.name), []).append(service)
        for (namespace, service_name), matches in matching_services.items():
            if len(matches) != 1:
                unresolved.append(UnresolvedReference(node.key, "workload_service_selector", service_name, namespace, "ambiguous duplicate Service declaration"))
                continue
            edge = InfraEdge(node.key, matches[0].key, "workload_service_selector", "inferred", "service selector matches workload pod labels", f"namespace:{node.namespace}")
            if edge not in edges:
                edges.append(edge)
    digest = hashlib.sha256("".join(sorted(g.source_digest for g in graphs)).encode()).hexdigest()
    return KubernetesGraph(tuple(nodes), tuple(edges), tuple(unresolved), digest, None)
