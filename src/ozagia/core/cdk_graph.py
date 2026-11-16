"""Offline reader for explicitly supplied AWS CDK cloud assembly metadata.

This module never invokes CDK, reads the filesystem, follows asset paths, or
contacts AWS. Callers provide the assembly ``manifest.json`` and optionally
the already-supplied template texts. Only allowlisted structural metadata is
returned; environment/account fields and arbitrary properties are discarded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

EXTRACTOR_VERSION = "cdk-assembly-source-v1"
MAX_MANIFEST_BYTES = 2_000_000
MAX_TEMPLATE_BYTES = 2_000_000
MAX_SUPPLIED_TEMPLATES = 256
MAX_TOTAL_TEMPLATE_BYTES = 16_000_000
MAX_ARTIFACTS = 10_000
SUPPORTED_SCHEMA_MAJORS = frozenset({30})
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")
_SAFE_TYPES = frozenset({"aws:cloudformation:stack", "aws:cdk:asset-manifest",
                         "aws:cdk:tree", "aws:cdk:metadata",
                         "cdk:asset-manifest"})


@dataclass(frozen=True)
class CdkArtifact:
    key: str
    artifact_id: str
    artifact_type: str
    source_digest: str
    provenance: str = "supplied_cdk_assembly"


@dataclass(frozen=True)
class CdkEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str


@dataclass(frozen=True)
class CdkResource:
    key: str
    stack_artifact: str
    logical_id: str
    resource_type: str
    template_path: str
    pointer: str
    source_digest: str
    provenance: str = "supplied_cdk_template"


@dataclass(frozen=True)
class CdkTemplateSource:
    key: str
    path: str
    source_digest: str
    provenance: str = "supplied_cdk_template"


@dataclass(frozen=True)
class CdkUnresolved:
    source: str
    relation: str
    target: str
    reason: str


@dataclass(frozen=True)
class CdkAssemblyGraph:
    schema_version: str | None
    artifacts: tuple[CdkArtifact, ...]
    edges: tuple[CdkEdge, ...]
    resources: tuple[CdkResource, ...]
    template_sources: tuple[CdkTemplateSource, ...]
    unresolved: tuple[CdkUnresolved, ...]
    diagnostics: tuple[str, ...]
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"extractor_version": EXTRACTOR_VERSION,
                "schema_version": self.schema_version,
                "manifest_digest": self.manifest_digest,
                "artifacts": [asdict(item) for item in self.artifacts],
                "edges": [asdict(item) for item in self.edges],
                "resources": [asdict(item) for item in self.resources],
                "template_sources": [asdict(item) for item in self.template_sources],
                "unresolved": [asdict(item) for item in self.unresolved],
                "diagnostics": list(self.diagnostics)}


class CdkAssemblyError(ValueError):
    pass


def _identity(value: Any) -> str | None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        return None
    # Do not accept identifiers that resemble URLs or path traversal.
    if ".." in value.split("/") or "://" in value:
        return None
    if re.search(r"(?<!\d)\d{12}(?!\d)", value):
        return None
    return value


def _relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _load_manifest(text: str) -> tuple[dict[str, Any], str]:
    raw = text.encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CdkAssemblyError("manifest size limit exceeded")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CdkAssemblyError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=unique_object)
    except CdkAssemblyError:
        raise
    except (ValueError, RecursionError) as exc:
        raise CdkAssemblyError("invalid assembly manifest JSON") from exc
    if not isinstance(data, dict):
        raise CdkAssemblyError("assembly manifest root must be an object")
    return data, hashlib.sha256(raw).hexdigest()


def read_cdk_assembly(
    manifest_json: str,
    *,
    templates: Mapping[str, str] | None = None,
    supported_schema_majors: frozenset[int] = SUPPORTED_SCHEMA_MAJORS,
) -> CdkAssemblyGraph:
    """Read supplied manifest metadata and optional supplied template texts.

    Only supported schema majors are accepted. Artifact dependency edges are
    emitted only for manifest artifacts. A stack ``templateFile`` is matched
    against the caller's in-memory ``templates`` mapping; the reader never
    opens that path itself. Resource identities are extracted through the
    source-only CloudFormation parser, and unresolved intrinsic references are
    retained as unresolved metadata.
    """
    manifest, digest = _load_manifest(manifest_json)
    version = manifest.get("version")
    if not isinstance(version, str) or not (match := _VERSION.fullmatch(version)):
        raise CdkAssemblyError("manifest version must be semantic x.y.z")
    major = int(match.group(1))
    if major not in supported_schema_majors:
        raise CdkAssemblyError(f"unsupported assembly schema major version: {major}")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, dict):
        raise CdkAssemblyError("manifest artifacts must be an object")
    if len(raw_artifacts) > MAX_ARTIFACTS:
        raise CdkAssemblyError("artifact count limit exceeded")

    artifacts: dict[str, CdkArtifact] = {}
    edges: list[CdkEdge] = []
    unresolved: list[CdkUnresolved] = []
    resources: list[CdkResource] = []
    template_sources: dict[str, CdkTemplateSource] = {}
    diagnostics: list[str] = []
    template_map: dict[str, str] = {}
    total_template_bytes = 0
    for raw_path, content in (templates or {}).items():
        if len(template_map) >= MAX_SUPPLIED_TEMPLATES:
            diagnostics.append("supplied template count limit exceeded")
            break
        safe_path = _relative_path(raw_path)
        if safe_path is None or not isinstance(content, str):
            diagnostics.append("unsafe or non-text supplied template skipped")
            continue
        size = len(content.encode("utf-8"))
        if size > MAX_TEMPLATE_BYTES:
            diagnostics.append(f"template size limit exceeded: {safe_path}")
            continue
        total_template_bytes += size
        if total_template_bytes > MAX_TOTAL_TEMPLATE_BYTES:
            diagnostics.append("supplied template byte limit exceeded")
            break
        template_map[safe_path] = content

    for artifact_id, info in raw_artifacts.items():
        safe_id = _identity(artifact_id)
        if safe_id is None or not isinstance(info, dict):
            diagnostics.append("invalid artifact entry skipped")
            continue
        kind = info.get("type")
        if kind not in _SAFE_TYPES:
            diagnostics.append(f"unsupported artifact type skipped: {safe_id}")
            continue
        key = "cdk:v1:" + hashlib.sha256(safe_id.encode("utf-8")).hexdigest()
        artifacts[safe_id] = CdkArtifact(key, safe_id, kind, digest)

    for artifact_id, info in raw_artifacts.items():
        if artifact_id not in artifacts or not isinstance(info, dict):
            continue
        source = artifacts[artifact_id].key
        dependencies = info.get("dependencies", [])
        if dependencies is not None and not isinstance(dependencies, list):
            diagnostics.append(f"invalid dependency list: {artifact_id}")
            dependencies = []
        for target_id in dependencies:
            target = artifacts.get(target_id) if isinstance(target_id, str) else None
            if target:
                edges.append(CdkEdge(source, target.key, "artifact_dependency",
                                     "declared", "manifest.dependencies"))
            else:
                raw_target = str(target_id)
                safe_target = "unresolved:" + hashlib.sha256(
                    raw_target.encode("utf-8", errors="replace")).hexdigest()[:16]
                unresolved.append(CdkUnresolved(source, "artifact_dependency",
                                                safe_target,
                                                "dependency artifact missing or invalid"))

        if info.get("type") != "aws:cloudformation:stack":
            continue
        props = info.get("properties")
        if not isinstance(props, dict):
            diagnostics.append(f"stack properties missing: {artifact_id}")
            continue
        template_path = _relative_path(props.get("templateFile"))
        if template_path is None:
            unresolved.append(CdkUnresolved(source, "stack_template",
                                            "<unsafe-or-missing-path>",
                                            "templateFile is not a safe relative path"))
            continue
        template = template_map.get(template_path)
        if template is None:
            unresolved.append(CdkUnresolved(source, "stack_template", template_path,
                                            "template was not supplied to reader"))
            continue
        from ozagia.core.cloudformation_graph import parse_cloudformation_source
        graph = parse_cloudformation_source(template, template_path)
        if graph.diagnostics:
            for location, message in graph.diagnostics:
                diagnostics.append(f"template {template_path}: {message}")
            continue
        template_key = "cdk-template:v1:" + hashlib.sha256(
            f"{artifact_id}\0{template_path}".encode()).hexdigest()
        template_sources.setdefault(template_key, CdkTemplateSource(
            template_key, template_path, graph.source_digest))
        edges.append(CdkEdge(source, template_key, "stack_template",
                             "resolved from imported artifact", "properties.templateFile"))
        for node in graph.nodes:
            if node.kind != "resource" or node.resource_type is None:
                continue
            resource_key = "cdk-resource:v1:" + hashlib.sha256(
                f"{artifact_id}\0{node.logical_id}".encode()).hexdigest()
            resources.append(CdkResource(resource_key, artifact_id, node.logical_id,
                                         node.resource_type, template_path,
                                         node.pointer, graph.source_digest))
        for ref in graph.unresolved:
            unresolved.append(CdkUnresolved(source, ref.relation,
                                            ref.target[:200], ref.reason))

    # Manifest stack metadata can include environment/account fields, assets,
    # commands, and arbitrary metadata. None are surfaced here.
    return CdkAssemblyGraph(version, tuple(artifacts.values()), tuple(edges),
                           tuple(resources), tuple(template_sources.values()),
                           tuple(unresolved), tuple(diagnostics), digest)
