"""Opt-in structural metadata for Helm charts and Kustomize overlays.

Only file names and a small allowlist of chart/build relationships are emitted.
Values, templates, rendered objects, arbitrary YAML fields, and remote content
are never returned. This module never executes Helm/Kustomize or fetches refs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping

EXTRACTOR_VERSION = "deployment-source-v1"
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_YAML_DEPTH = 64


@dataclass(frozen=True)
class DeploymentNode:
    key: str
    kind: str
    path: str
    name: str
    source_digest: str
    provenance: str = "declared"
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(frozen=True)
class DeploymentEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class UnresolvedDeploymentReference:
    source: str
    relation: str
    target: str
    evidence: str
    reason: str


@dataclass(frozen=True)
class DeploymentGraph:
    nodes: tuple[DeploymentNode, ...]
    edges: tuple[DeploymentEdge, ...]
    unresolved: tuple[UnresolvedDeploymentReference, ...]
    diagnostics: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"extractor_version": EXTRACTOR_VERSION,
                "nodes": [asdict(node) for node in self.nodes],
                "edges": [asdict(edge) for edge in self.edges],
                "unresolved": [asdict(ref) for ref in self.unresolved],
                "diagnostics": [list(d) for d in self.diagnostics]}


def _safe_path(raw: str) -> str | None:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    return path.as_posix()


def _yaml(text: str) -> Any:
    """Parse bounded YAML with aliases and duplicate keys rejected."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValueError("YAML parser unavailable; install the openapi-yaml extra") from exc

    class Loader(yaml.SafeLoader):
        def __init__(self, stream: str):
            self.alias_count = 0
            super().__init__(stream)

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                self.alias_count += 1
                raise ValueError("YAML aliases are unsupported")
            return super().compose_node(parent, index)

        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            if isinstance(node, yaml.MappingNode):
                seen: set[Any] = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if key in seen:
                        raise ValueError("duplicate YAML key")
                    seen.add(key)
            return super().construct_mapping(node, deep)

    try:
        value = yaml.load(text, Loader=Loader)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid or unsupported YAML") from exc
    if _depth(value) > MAX_YAML_DEPTH:
        raise ValueError("YAML nesting limit exceeded")
    return value


def _depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_YAML_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([depth] + [_depth(k, depth + 1) for k in value]
                   + [_depth(v, depth + 1) for v in value.values()])
    if isinstance(value, list):
        return max([depth] + [_depth(v, depth + 1) for v in value])
    return depth


def _node(kind: str, path: str, name: str, content: str = "") -> DeploymentNode:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    key = "deploy:v1:" + hashlib.sha256(
        "\0".join((kind, path, name)).encode("utf-8")).hexdigest()
    return DeploymentNode(key, kind, path, name, digest)


def helm_template_paths(files: Mapping[str, str]) -> frozenset[str]:
    """Identify chart template sources without parsing or rendering their YAML."""
    chart_roots = {
        PurePosixPath(path).parent.as_posix()
        for path in files
        if PurePosixPath(path).name == "Chart.yaml"
    }
    result = set()
    for raw_path, content in files.items():
        path = _safe_path(raw_path)
        if path is None:
            continue
        under_chart_templates = any(
            path.startswith((root + "/" if root != "." else "") + "templates/")
            for root in chart_roots
        )
        is_yaml = path.lower().endswith((".yaml", ".yml"))
        if under_chart_templates or (is_yaml and ("{{" in content or "{%" in content)):
            result.add(path)
    return frozenset(result)


def extract_deployment_metadata(files: Mapping[str, str]) -> DeploymentGraph:
    """Extract relationships from an in-memory repo-relative file mapping.

    Helm charts link to allowlisted conventional values files and template file
    paths. Kustomize bases/resources and patches resolve only to supplied local
    paths. External and absent paths are returned as unresolved references.
    Raw file content is used transiently for bounded parsing and hashing only.
    """
    if len(files) > MAX_FILES:
        raise ValueError("file count limit exceeded")
    normalized: dict[str, str] = {}
    diagnostics: list[tuple[str, str]] = []
    for raw_path, content in files.items():
        path = _safe_path(raw_path)
        if path is None or not isinstance(content, str):
            diagnostics.append((str(raw_path), "unsafe path or non-text source skipped"))
            continue
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            diagnostics.append((path, "file size limit exceeded"))
            continue
        normalized[path] = content

    nodes: list[DeploymentNode] = []
    edges: list[DeploymentEdge] = []
    unresolved: list[UnresolvedDeploymentReference] = []
    by_path: dict[str, DeploymentNode] = {}

    def add_node(kind: str, path: str, name: str, content: str = "") -> DeploymentNode:
        node = _node(kind, path, name, content)
        if node.key not in {existing.key for existing in nodes}:
            nodes.append(node)
        by_path[path] = node
        return node

    # Charts are discovered from Chart.yaml. Only name/version and dependency
    # names are inspected; dependency URLs and values are never copied out.
    chart_roots: dict[str, DeploymentNode] = {}
    for path, content in sorted(normalized.items()):
        if PurePosixPath(path).name != "Chart.yaml":
            continue
        try:
            data = _yaml(content)
            if not isinstance(data, dict):
                raise ValueError("chart metadata must be a mapping")
            name = data.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("chart name missing")
        except (ValueError, RecursionError) as exc:
            diagnostics.append((path, str(exc)))
            continue
        root = PurePosixPath(path).parent.as_posix()
        chart = add_node("helm_chart", path, name.strip(), content)
        chart_roots[root] = chart
        for dep in data.get("dependencies", []) if isinstance(data.get("dependencies"), list) else []:
            dep_name = dep.get("name") if isinstance(dep, dict) else None
            if isinstance(dep_name, str):
                unresolved.append(UnresolvedDeploymentReference(
                    chart.key, "helm_dependency", dep_name,
                    "Chart.yaml#/dependencies", "dependency is not resolved or fetched"))

    for root, chart in sorted(chart_roots.items()):
        prefix = "" if root == "." else root + "/"
        for path, content in sorted(normalized.items()):
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if rel in {"values.yaml", "values.yml"} or (
                    rel.startswith("values.") and rel.endswith((".yaml", ".yml"))):
                value_node = add_node("helm_values_file", path, PurePosixPath(path).name, content)
                edges.append(DeploymentEdge(chart.key, value_node.key, "chart_values_source",
                                            "declared", "conventional chart values path", root))
            elif rel.startswith("templates/") and not rel.endswith("/"):
                template = add_node("helm_template_source", path, PurePosixPath(path).name, content)
                edges.append(DeploymentEdge(chart.key, template.key, "chart_template_source",
                                            "declared", "template file path; not rendered", root))

    # Kustomize reads only structural path fields. Values, labels and patches'
    # contents are not retained or interpreted.
    for path, content in sorted(normalized.items()):
        if PurePosixPath(path).name not in {"kustomization.yaml", "kustomization.yml", "kustomization.json"}:
            continue
        try:
            data = json.loads(content) if path.endswith(".json") else _yaml(content)
            if not isinstance(data, dict):
                raise ValueError("kustomization must be a mapping")
        except (ValueError, RecursionError) as exc:
            diagnostics.append((path, str(exc)))
            continue
        owner = add_node("kustomize_overlay", path, PurePosixPath(path).parent.name or ".", content)
        refs: list[tuple[str, str]] = []
        for field in ("resources", "bases", "components"):
            values = data.get(field)
            if isinstance(values, list):
                refs.extend(("kustomize_base_or_resource", v) for v in values if isinstance(v, str))
        patches = data.get("patches")
        if isinstance(patches, list):
            for patch in patches:
                if isinstance(patch, str):
                    refs.append(("kustomize_patch", patch))
                elif isinstance(patch, dict) and isinstance(patch.get("path"), str):
                    refs.append(("kustomize_patch", patch["path"]))
        for field in ("patchesStrategicMerge", "patchesJson6902"):
            values = data.get(field)
            if isinstance(values, list):
                for patch in values:
                    if isinstance(patch, str):
                        refs.append(("kustomize_patch", patch))
                    elif isinstance(patch, dict) and isinstance(patch.get("path"), str):
                        refs.append(("kustomize_patch", patch["path"]))
        for relation, target in refs:
            if "{{" in target or "}}" in target:
                unresolved.append(UnresolvedDeploymentReference(owner.key, relation,
                    target[:160], "kustomization path", "templated path"))
                continue
            if target.startswith(("https://", "http://", "git::", "ssh://")):
                unresolved.append(UnresolvedDeploymentReference(owner.key, relation,
                    hashlib.sha256(target.encode()).hexdigest()[:16], "kustomization path",
                    "remote reference not fetched"))
                continue
            candidate = _safe_path((PurePosixPath(path).parent / target).as_posix())
            if candidate is None:
                unresolved.append(UnresolvedDeploymentReference(owner.key, relation,
                    "<unsafe-path>", "kustomization path", "path escapes repository"))
                continue
            target_path = candidate
            if target_path not in normalized:
                # A resource can be a local directory containing its own
                # kustomization; directory links use the known manifest path.
                directory_manifest = str(PurePosixPath(target_path) / "kustomization.yaml")
                target_path = directory_manifest if directory_manifest in normalized else target_path
            target_node = by_path.get(target_path)
            if target_node is None and target_path in normalized:
                target_node = add_node("kustomize_source", target_path,
                                       PurePosixPath(target_path).name, normalized[target_path])
            if target_node:
                edges.append(DeploymentEdge(owner.key, target_node.key, relation,
                                            "declared", "kustomization path field", path))
            else:
                unresolved.append(UnresolvedDeploymentReference(owner.key, relation,
                    target_path, "kustomization path field", "local target missing"))

    return DeploymentGraph(tuple(nodes), tuple(edges), tuple(unresolved), tuple(diagnostics))
