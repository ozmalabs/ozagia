"""Bounded static metadata extraction for Argo CD and Flux declarations.

No network access or manifest rendering occurs here. Only explicitly
allowlisted GitOps identity and source/target metadata is retained.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

EXTRACTOR_VERSION = "gitops-source-v1"
MAX_SOURCE_BYTES = 2_000_000
MAX_DOCUMENTS = 256


@dataclass(frozen=True)
class GitOpsNode:
    key: str
    kind: str
    name: str
    namespace: str | None
    path: str
    pointer: str
    source_digest: str
    api_version: str
    source_kind: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    source_path: str | None = None
    target_revision: str | None = None
    destination_name: str | None = None
    destination_server: str | None = None
    destination_namespace: str | None = None


@dataclass(frozen=True)
class GitOpsEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class GitOpsUnresolved:
    source: str
    relation: str
    target_name: str
    target_kind: str | None
    namespace: str | None
    evidence: str


@dataclass(frozen=True)
class GitOpsGraph:
    source_path: str
    source_digest: str
    nodes: tuple[GitOpsNode, ...]
    edges: tuple[GitOpsEdge, ...]
    unresolved: tuple[GitOpsUnresolved, ...]
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_digest": self.source_digest,
            "extractor_version": EXTRACTOR_VERSION,
            "nodes": [{
                "key": node.key,
                "kind": node.kind,
                "name": node.name,
                "namespace": node.namespace,
                "path": node.path,
                "pointer": node.pointer,
                "source_digest": node.source_digest,
                "api_version": node.api_version,
                "source_kind": node.source_kind,
                "source_name": node.source_name,
                # URL paths and revision strings can carry credentials/tokens;
                # only the pre-sanitized origin is exposed.
                "source_url": node.source_url,
                "target_revision": None,
                "destination_name": node.destination_name,
                "destination_server": node.destination_server,
                "destination_namespace": node.destination_namespace,
            } for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
            "unresolved": [asdict(ref) for ref in self.unresolved],
            "diagnostics": list(self.diagnostics),
        }


class GitOpsParseError(ValueError):
    pass


def _safe_text(value: Any, *, limit: int = 1024) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(ch) < 32 for ch in value):
        return None
    return value


def _safe_url(value: Any) -> str | None:
    raw = _safe_text(value)
    if raw is None:
        return None
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.hostname:
            return None
        # Drop userinfo, query and fragment, which commonly carry credentials.
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, "", "", ""))[:1024]
    except ValueError:
        return None


def _documents(source: str, path: str, max_documents: int) -> list[Any]:
    if path.lower().endswith(".json"):
        try:
            value = json.loads(source)
        except (ValueError, RecursionError) as exc:
            raise GitOpsParseError("invalid JSON manifest") from exc
        docs = value if isinstance(value, list) else [value]
    else:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GitOpsParseError("YAML parser unavailable; install the openapi-yaml extra") from exc

        class Loader(yaml.SafeLoader):
            def __init__(self, stream: str):
                super().__init__(stream)

            def compose_node(self, parent: Any, index: Any) -> Any:
                if self.check_event(yaml.AliasEvent):
                    raise GitOpsParseError("YAML aliases are unsupported")
                return super().compose_node(parent, index)

            def construct_mapping(self, node: Any, deep: bool = False) -> Any:
                seen: set[Any] = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if key in seen:
                        raise GitOpsParseError("duplicate YAML mapping key")
                    seen.add(key)
                return super().construct_mapping(node, deep=deep)

        try:
            docs = list(yaml.load_all(source, Loader=Loader))
        except GitOpsParseError:
            raise
        except Exception as exc:
            raise GitOpsParseError("invalid or unsupported YAML manifest") from exc
    docs = [doc for doc in docs if doc is not None]
    if len(docs) > max_documents:
        raise GitOpsParseError("manifest document limit exceeded")
    if not all(isinstance(doc, dict) for doc in docs):
        raise GitOpsParseError("manifest document must be a mapping")
    return docs


def parse_gitops_source(
    source: str,
    path: str,
    *,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_documents: int = MAX_DOCUMENTS,
) -> GitOpsGraph:
    """Extract Argo Application and Flux source/deployment declarations.

    Flux source references resolve only against declarations in this input.
    Argo repository sources and Flux remote URLs are always marked unresolved;
    this parser never fetches a source or claims a deployment is live.
    """
    raw = source.encode("utf-8")
    if len(raw) > max_source_bytes:
        raise GitOpsParseError("manifest source size limit exceeded")
    path = PurePosixPath(path).as_posix()
    digest = hashlib.sha256(raw).hexdigest()
    docs = _documents(source, path, max_documents)
    nodes: list[GitOpsNode] = []
    refs: list[tuple[GitOpsNode, str, str, str | None, str]] = []
    diagnostics: list[str] = []
    for index, doc in enumerate(docs):
        api = _safe_text(doc.get("apiVersion"), limit=256)
        kind = _safe_text(doc.get("kind"), limit=128)
        meta = doc.get("metadata")
        if not api or not kind or not isinstance(meta, dict):
            continue
        name = _safe_text(meta.get("name"), limit=256)
        namespace = _safe_text(meta.get("namespace"), limit=256)
        if not name:
            continue
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        source_kind = source_name = source_url = source_path = revision = None
        flux_ref: tuple[str, str] | None = None
        destination_name = destination_server = destination_namespace = None
        if kind == "Application" and api.startswith("argoproj.io/"):
            argo_source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
            sources = spec.get("sources") if isinstance(spec.get("sources"), list) else None
            destination = spec.get("destination") if isinstance(spec.get("destination"), dict) else {}
            if sources:
                diagnostics.append(f"{path}#/documents/{index}: Argo multi-source entries are unsupported and were not indexed")
            source_url = _safe_url(argo_source.get("repoURL"))
            source_path = _safe_text(argo_source.get("path"), limit=512)
            # Revision is intentionally omitted: refs are arbitrary user strings
            # and may themselves contain credentials or tokens.
            revision = None
            destination_name = _safe_text(destination.get("name"), limit=256)
            destination_server = _safe_url(destination.get("server"))
            destination_namespace = _safe_text(destination.get("namespace"), limit=256)
        elif api.startswith("source.toolkit.fluxcd.io/") and kind in {"GitRepository", "HelmRepository", "Bucket", "OCIRepository"}:
            source_kind = kind
            source_name = name
            url = spec.get("url")
            source_url = _safe_url(url)
            revision = None
            if source_url is None:
                diagnostics.append(f"{path}#/documents/{index}: unsupported or unsafe source URL metadata")
        elif (api.startswith("kustomize.toolkit.fluxcd.io/") and kind == "Kustomization") or (api.startswith("helm.toolkit.fluxcd.io/") and kind == "HelmRelease"):
            source_ref = spec.get("sourceRef") if isinstance(spec.get("sourceRef"), dict) else {}
            source_kind = _safe_text(source_ref.get("kind"), limit=128)
            source_name = _safe_text(source_ref.get("name"), limit=256)
            source_path = _safe_text(spec.get("path"), limit=512) if kind == "Kustomization" else None
            target = spec.get("targetNamespace")
            destination_namespace = _safe_text(target, limit=256)
            if source_name and source_kind:
                flux_ref = (source_kind, source_name)
        else:
            continue
        key = "gitops:v1:" + hashlib.sha256("\0".join((path, str(index), api, kind, namespace or "", name)).encode()).hexdigest()
        node = GitOpsNode(key, kind, name, namespace, path, f"/documents/{index}", digest, api, source_kind, source_name, source_url, source_path, revision, destination_name, destination_server, destination_namespace)
        nodes.append(node)
        if source_url:
            refs.append((node, "remote_source", source_url, namespace, "remote_source"))
        elif flux_ref:
            refs.append((node, flux_ref[0], flux_ref[1], namespace, "flux_source_ref"))

    edges: list[GitOpsEdge] = []
    unresolved: list[GitOpsUnresolved] = []
    for source_node, target_kind, target_name, namespace, relation in refs:
        if relation == "remote_source":
            unresolved.append(GitOpsUnresolved(source_node.key, relation, target_name, None, namespace, "remote source metadata only; no fetch"))
            continue
        if namespace is None:
            unresolved.append(GitOpsUnresolved(source_node.key, relation, target_name, target_kind, None, "namespace unknown"))
            continue
        candidates = [n for n in nodes if n.kind == target_kind and n.name == target_name and n.namespace == namespace]
        if len(candidates) == 1:
            edges.append(GitOpsEdge(source_node.key, candidates[0].key, "flux_source_ref", "declared", "spec.sourceRef kind/name", f"namespace:{namespace or '<unknown>'}"))
        else:
            unresolved.append(GitOpsUnresolved(source_node.key, "flux_source_ref", target_name, target_kind, namespace, "missing source declaration" if not candidates else "ambiguous source declaration"))
    # Argo source and destination are metadata on the declaration, not proof of
    # successful rendering, sync, or cluster membership.
    for node in nodes:
        if node.kind == "Application":
            if node.source_url:
                unresolved.append(GitOpsUnresolved(node.key, "argo_source", node.source_url, None, node.namespace, "remote repository metadata only; no fetch"))
            if node.source_url is None and node.source_path:
                unresolved.append(GitOpsUnresolved(node.key, "argo_source", node.source_path, None, node.namespace, "source URL unsupported or omitted"))
    return GitOpsGraph(path, digest, tuple(nodes), tuple(edges), tuple(unresolved), tuple(diagnostics))
