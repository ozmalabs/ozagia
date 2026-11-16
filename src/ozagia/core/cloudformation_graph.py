"""Bounded, source-only CloudFormation structure extraction.

The extractor keeps logical identifiers, resource types, source pointers and
digests. It never returns template values, property contents, defaults, outputs,
or secret-bearing strings. It does not evaluate intrinsic functions or execute
CloudFormation transforms/macros.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

EXTRACTOR_VERSION = "cloudformation-source-v1"
MAX_SOURCE_BYTES = 2_000_000
MAX_NODES = 20_000
MAX_DEPTH = 64
_LOGICAL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,254}$")
_SUBSTITUTION = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class CloudFormationNode:
    key: str
    kind: str
    logical_id: str
    resource_type: str | None
    path: str
    pointer: str
    source_digest: str
    provenance: str = "declared"
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(frozen=True)
class CloudFormationEdge:
    source: str
    target: str
    relation: str
    provenance: str
    evidence: str
    scope: str


@dataclass(frozen=True)
class UnresolvedCloudFormationReference:
    source: str
    relation: str
    target: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class CloudFormationGraph:
    nodes: tuple[CloudFormationNode, ...]
    edges: tuple[CloudFormationEdge, ...]
    unresolved: tuple[UnresolvedCloudFormationReference, ...]
    diagnostics: tuple[tuple[str, str], ...]
    source_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"extractor_version": EXTRACTOR_VERSION,
                "source_digest": self.source_digest,
                "nodes": [asdict(node) for node in self.nodes],
                "edges": [asdict(edge) for edge in self.edges],
                "unresolved": [asdict(ref) for ref in self.unresolved],
                "diagnostics": [list(row) for row in self.diagnostics]}


class CloudFormationParseError(ValueError):
    """Invalid, unsupported, or over-limit source document."""


def _depth(value: Any, level: int = 0, seen: set[int] | None = None) -> int:
    if level > MAX_DEPTH:
        return level
    if seen is None:
        seen = set()
    if isinstance(value, (dict, list)):
        ident = id(value)
        if ident in seen:  # alias cycle
            raise CloudFormationParseError("YAML alias cycle is unsupported")
        seen.add(ident)
        vals = value.keys() if isinstance(value, dict) else value
        child = max((_depth(v, level + 1, seen) for v in vals), default=level)
        if isinstance(value, dict):
            child = max(child, max((_depth(v, level + 1, seen)
                                    for v in value.values()), default=level))
        seen.remove(ident)
        return child
    return level


def _load_yaml(source: str) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CloudFormationParseError(
            "YAML parser unavailable; install the openapi-yaml extra") from exc

    class Loader(yaml.SafeLoader):
        alias_count = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                self.alias_count += 1
                raise CloudFormationParseError("YAML aliases are unsupported")
            return super().compose_node(parent, index)

        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            if isinstance(node, yaml.MappingNode):
                keys: set[Any] = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if key in keys:
                        raise CloudFormationParseError("duplicate YAML mapping key")
                    keys.add(key)
            return super().construct_mapping(node, deep)

    def intrinsic(loader: Any, suffix: str, node: Any) -> dict[str, Any]:
        # Preserve the tag shape transiently for the allowlisted reference walk.
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {"__cfn_intrinsic__": suffix, "__value__": value}

    Loader.add_multi_constructor("!", intrinsic)
    try:
        docs = list(yaml.load_all(source, Loader=Loader))
    except CloudFormationParseError:
        raise
    except Exception as exc:
        raise CloudFormationParseError("invalid or unsupported CloudFormation YAML") from exc
    docs = [doc for doc in docs if doc is not None]
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise CloudFormationParseError("expected one CloudFormation mapping document")
    return docs[0]


def _load(source: str, path: str) -> Any:
    raw = source.encode("utf-8")
    if len(raw) > MAX_SOURCE_BYTES:
        raise CloudFormationParseError("template source size limit exceeded")
    if path.lower().endswith(".json"):
        try:
            def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise CloudFormationParseError("duplicate JSON object key")
                    result[key] = value
                return result
            data = json.loads(source, object_pairs_hook=unique_object)
        except CloudFormationParseError:
            raise
        except (ValueError, RecursionError) as exc:
            raise CloudFormationParseError("invalid CloudFormation JSON") from exc
    else:
        data = _load_yaml(source)
    if not isinstance(data, dict):
        raise CloudFormationParseError("template root must be a mapping")
    if _depth(data) > MAX_DEPTH:
        raise CloudFormationParseError("template nesting limit exceeded")
    return data


def _pointer(path: str) -> str:
    return path


def parse_cloudformation_source(source: str, path: str) -> CloudFormationGraph:
    """Extract static resource, parameter, output and reference structure.

    Static ``Ref``, ``Fn::GetAtt``, ``Fn::Sub`` logical identifiers and
    ``DependsOn`` declarations are represented as edges. Other intrinsic forms
    and references to absent logical IDs are returned as unresolved metadata.
    No runtime parameters, defaults, resource properties or output values are
    included in the result.
    """
    safe_path = PurePosixPath(path.replace("\\", "/"))
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise CloudFormationParseError("template path must be repository-relative")
    path = safe_path.as_posix()
    raw = source.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        template = _load(source, path)
    except CloudFormationParseError as exc:
        return CloudFormationGraph((), (), (), ((path, str(exc)),), digest)

    nodes: list[CloudFormationNode] = []
    diagnostics: list[tuple[str, str]] = []
    identifiers: dict[str, CloudFormationNode] = {}

    def declarations(section: str, kind: str) -> None:
        values = template.get(section, {})
        if values is None:
            return
        if not isinstance(values, dict):
            diagnostics.append((f"/{section}", "section must be a mapping"))
            return
        for logical_id, body in values.items():
            if not isinstance(logical_id, str) or not _LOGICAL_ID.fullmatch(logical_id):
                diagnostics.append((f"/{section}", "invalid logical identifier skipped"))
                continue
            if logical_id in identifiers:
                diagnostics.append((f"/{section}/{logical_id}", "duplicate logical identifier"))
                continue
            if len(nodes) >= MAX_NODES:
                diagnostics.append((f"/{section}", "node count limit exceeded"))
                return
            resource_type = None
            if kind == "resource":
                resource_type = body.get("Type") if isinstance(body, dict) else None
                if not isinstance(resource_type, str):
                    diagnostics.append((f"/{section}/{logical_id}", "resource Type missing"))
                    continue
            pointer = f"/{section}/{logical_id}"
            key = "cfn:v1:" + hashlib.sha256(
                f"{path}\0{logical_id}".encode("utf-8")).hexdigest()
            node = CloudFormationNode(key, kind, logical_id, resource_type,
                                      path, pointer, digest)
            nodes.append(node)
            identifiers[logical_id] = node

    declarations("Resources", "resource")
    declarations("Parameters", "parameter")
    declarations("Outputs", "output")
    edges: list[CloudFormationEdge] = []
    unresolved: list[UnresolvedCloudFormationReference] = []

    # Retain body references transiently only. Unknown template sections and
    # arbitrary values do not appear in outputs.
    sections = (("Resources", "resource"), ("Outputs", "output"))

    def record_ref(source_node: CloudFormationNode, target: Any, relation: str,
                   evidence: str) -> None:
        if not isinstance(target, str) or not target:
            unresolved.append(UnresolvedCloudFormationReference(
                source_node.key, relation, "<dynamic>", "dynamic reference target", evidence))
            return
        if target in identifiers:
            edges.append(CloudFormationEdge(source_node.key, identifiers[target].key,
                                            relation, "declared", evidence, path))
        else:
            # Retain only a valid logical-identifier shape; never an arbitrary
            # template value that a malformed Ref/GetAtt might carry.
            safe_target = target if _LOGICAL_ID.fullmatch(target) else "<invalid-logical-id>"
            unresolved.append(UnresolvedCloudFormationReference(
                source_node.key, relation, safe_target,
                "logical identifier not declared", evidence))

    def walk(node: CloudFormationNode, value: Any, pointer: str) -> None:
        if isinstance(value, list):
            for i, child in enumerate(value):
                walk(node, child, f"{pointer}/{i}")
            return
        if not isinstance(value, dict):
            return
        # Short YAML intrinsic tags are normalized by the loader.
        if "__cfn_intrinsic__" in value:
            tag = value.get("__cfn_intrinsic__")
            inner = value.get("__value__")
            alias = {"Ref": "Ref", "GetAtt": "Fn::GetAtt", "Sub": "Fn::Sub"}.get(tag)
            if alias:
                walk(node, {alias: inner}, pointer)
            else:
                unresolved.append(UnresolvedCloudFormationReference(
                    node.key, "intrinsic", str(tag)[:80], "unsupported intrinsic tag", pointer))
            return
        for key, child in value.items():
            if key == "Ref":
                record_ref(node, child, "ref", pointer + "/Ref")
            elif key == "Fn::GetAtt":
                target = child[0] if isinstance(child, list) and child else None
                record_ref(node, target, "get_att", pointer + "/Fn::GetAtt")
            elif key == "Fn::Sub":
                expression = child[0] if isinstance(child, list) and child else child
                overrides = child[1] if isinstance(child, list) and len(child) > 1 else {}
                if isinstance(expression, str):
                    for match in _SUBSTITUTION.finditer(expression):
                        ref = match.group(1).split(".", 1)[0]
                        if (ref.startswith(("!", "AWS::"))
                                or (isinstance(overrides, dict) and ref in overrides)):
                            continue
                        if not _LOGICAL_ID.fullmatch(ref):
                            unresolved.append(UnresolvedCloudFormationReference(
                                node.key, "substitution", "<dynamic>",
                                "non-static Fn::Sub expression", pointer + "/Fn::Sub"))
                        else:
                            record_ref(node, ref, "substitution", pointer + "/Fn::Sub")
                else:
                    unresolved.append(UnresolvedCloudFormationReference(
                        node.key, "substitution", "<dynamic>",
                        "non-static Fn::Sub expression", pointer + "/Fn::Sub"))
            elif isinstance(key, str) and key.startswith("Fn::"):
                unresolved.append(UnresolvedCloudFormationReference(
                    node.key, "intrinsic", key, "unsupported intrinsic function", pointer + "/" + key))
            elif isinstance(child, (dict, list)):
                walk(node, child, pointer + "/" + str(key))

    for section, kind in sections:
        values = template.get(section, {})
        if not isinstance(values, dict):
            continue
        for logical_id, body in values.items():
            node = identifiers.get(logical_id) if isinstance(logical_id, str) else None
            if node is None or not isinstance(body, dict):
                continue
            if kind == "resource" and "DependsOn" in body:
                deps = body["DependsOn"]
                for dep in deps if isinstance(deps, list) else [deps]:
                    record_ref(node, dep, "depends_on", node.pointer + "/DependsOn")
            walk(node, body, node.pointer)

    # Stable de-duplication preserves deterministic source ordering.
    edges = list(dict.fromkeys(edges))
    unresolved = list(dict.fromkeys(unresolved))
    return CloudFormationGraph(tuple(nodes), tuple(edges), tuple(unresolved),
                               tuple(diagnostics), digest)
