"""Bounded source-only extraction for Pulumi YAML resource declarations.

This does not load a Pulumi program, resolve configuration, evaluate helpers,
contact a backend, or inspect stack state. Only logical resource names, type
tokens, static resource interpolations and source digests are returned.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import PurePosixPath
import re
from typing import Any

EXTRACTOR_VERSION = "pulumi-yaml-source-v1"
MAX_SOURCE_BYTES = 1_000_000
MAX_RESOURCES = 10_000
MAX_DEPTH = 64
_LOGICAL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_RESOURCE_TYPE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+:[A-Za-z0-9_.-]+$")
_INTERPOLATION = re.compile(r"\$\{([^{}]+)\}")


@dataclass(frozen=True)
class PulumiNode:
    key: str
    logical_name: str
    resource_type: str
    path: str
    pointer: str
    source_digest: str
    provenance: str = "declared"
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(frozen=True)
class PulumiEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class PulumiUnresolvedReference:
    source: str
    relation: str
    target_fingerprint: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class PulumiGraph:
    nodes: tuple[PulumiNode, ...]
    edges: tuple[PulumiEdge, ...]
    unresolved: tuple[PulumiUnresolvedReference, ...]
    diagnostics: tuple[tuple[str, str], ...]
    source_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"extractor_version": EXTRACTOR_VERSION,
                "source_digest": self.source_digest,
                "nodes": [asdict(item) for item in self.nodes],
                "edges": [asdict(item) for item in self.edges],
                "unresolved": [asdict(item) for item in self.unresolved],
                "diagnostics": [list(item) for item in self.diagnostics]}


class PulumiParseError(ValueError):
    pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _depth(value: Any, level: int = 0, seen: set[int] | None = None) -> int:
    if level > MAX_DEPTH:
        return level
    if seen is None:
        seen = set()
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in seen:
            raise PulumiParseError("YAML alias cycle is unsupported")
        seen.add(identity)
        items = value.keys() if isinstance(value, dict) else value
        child_depth = max((_depth(item, level + 1, seen) for item in items),
                          default=level)
        if isinstance(value, dict):
            child_depth = max(child_depth,
                              max((_depth(item, level + 1, seen)
                                   for item in value.values()), default=level))
        seen.remove(identity)
        return child_depth
    return level


def _load(source: str) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PulumiParseError("YAML parser unavailable; install the openapi-yaml extra") from exc

    class Loader(yaml.SafeLoader):
        alias_count = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                self.alias_count += 1
                raise PulumiParseError("YAML aliases are unsupported")
            return super().compose_node(parent, index)

        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            if isinstance(node, yaml.MappingNode):
                keys: set[Any] = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if key in keys:
                        raise PulumiParseError("duplicate YAML mapping key")
                    keys.add(key)
            return super().construct_mapping(node, deep)

    try:
        result = yaml.load(source, Loader=Loader)
    except PulumiParseError:
        raise
    except Exception as exc:
        raise PulumiParseError("invalid or unsupported Pulumi YAML") from exc
    if not isinstance(result, dict):
        raise PulumiParseError("Pulumi YAML root must be a mapping")
    if _depth(result) > MAX_DEPTH:
        raise PulumiParseError("YAML nesting limit exceeded")
    return result


def parse_pulumi_yaml(source: str, path: str = "Pulumi.yaml") -> PulumiGraph:
    """Extract resource nodes and static ``${resource.attribute}`` edges.

    Pulumi config, outputs, provider options and all property values are omitted.
    Interpolations matching a declared logical resource name produce a declared
    edge; dynamic, malformed and non-resource expressions are fingerprinted in
    unresolved results so their literal text is not persisted or emitted.
    """
    normalized_path = PurePosixPath(path.replace("\\", "/"))
    if normalized_path.is_absolute() or ".." in normalized_path.parts:
        raise PulumiParseError("Pulumi source path must be repository-relative")
    path = normalized_path.as_posix()
    raw = source.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_SOURCE_BYTES:
        return PulumiGraph((), (), (), ((path, "source size limit exceeded"),), digest)
    try:
        document = _load(source)
    except PulumiParseError as exc:
        return PulumiGraph((), (), (), ((path, str(exc)),), digest)

    raw_resources = document.get("resources", {})
    if raw_resources is None:
        raw_resources = {}
    if not isinstance(raw_resources, dict):
        return PulumiGraph((), (), (), (("/resources", "resources must be a mapping"),), digest)
    if len(raw_resources) > MAX_RESOURCES:
        return PulumiGraph((), (), (), (("/resources", "resource count limit exceeded"),), digest)

    nodes: list[PulumiNode] = []
    by_name: dict[str, PulumiNode] = {}
    diagnostics: list[tuple[str, str]] = []
    for name, declaration in raw_resources.items():
        if not isinstance(name, str) or not _LOGICAL_NAME.fullmatch(name):
            diagnostics.append(("/resources", "invalid logical resource name skipped"))
            continue
        if not isinstance(declaration, dict):
            diagnostics.append((f"/resources/{name}", "resource declaration must be a mapping"))
            continue
        typ = declaration.get("type")
        if not isinstance(typ, str) or not _RESOURCE_TYPE.fullmatch(typ):
            diagnostics.append((f"/resources/{name}", "resource type missing or unsupported"))
            continue
        key = "pulumi:v1:" + _hash(f"{path}\0{name}")
        node = PulumiNode(key, name, typ, path, f"/resources/{name}", digest)
        nodes.append(node)
        by_name[name] = node

    edges: list[PulumiEdge] = []
    unresolved: list[PulumiUnresolvedReference] = []

    def inspect_value(owner: PulumiNode, value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.lower().startswith("fn::"):
                    unresolved.append(PulumiUnresolvedReference(
                        owner.key, "dynamic_expression", _hash(key),
                        "unsupported function expression",
                        owner.pointer + "/properties"))
                inspect_value(owner, child, pointer)
        elif isinstance(value, list):
            for child in value:
                inspect_value(owner, child, pointer)
        elif isinstance(value, str):
            for match in _INTERPOLATION.finditer(value):
                expr = match.group(1).strip()
                # Resource reference grammar: logical resource name followed by
                # one or more attribute path components. Config/stack refs and
                # function calls are intentionally unresolved.
                parts = expr.split(".")
                target_name = parts[0] if len(parts) >= 2 else ""
                if len(parts) >= 2 and _LOGICAL_NAME.fullmatch(target_name):
                    target = by_name.get(target_name)
                    if target:
                        edges.append(PulumiEdge(owner.key, target.key,
                                                "resource_interpolation", "declared",
                                                owner.pointer + "/properties", path))
                    else:
                        unresolved.append(PulumiUnresolvedReference(
                            owner.key, "resource_interpolation", _hash(expr),
                            "referenced resource is not declared",
                            owner.pointer + "/properties"))
                else:
                    unresolved.append(PulumiUnresolvedReference(
                        owner.key, "interpolation", _hash(expr),
                        "config, stack, function, or dynamic interpolation",
                        owner.pointer + "/properties"))

    # Property expressions are visited transiently; resource options, config,
    # outputs, provider setup, stack settings and literal secrets stay omitted.
    for name, declaration in raw_resources.items():
        owner = by_name.get(name) if isinstance(name, str) else None
        if owner is None:
            continue
        properties = declaration.get("properties", {})
        if isinstance(properties, dict):
            inspect_value(owner, properties, owner.pointer + "/properties")
        elif properties is not None:
            diagnostics.append((owner.pointer + "/properties",
                                "properties must be a mapping; references not scanned"))

    edges = list(dict.fromkeys(edges))
    unresolved = list(dict.fromkeys(unresolved))
    return PulumiGraph(tuple(nodes), tuple(edges), tuple(unresolved),
                       tuple(diagnostics), digest)
