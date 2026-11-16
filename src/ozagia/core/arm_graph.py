"""Safe structural extraction for ARM JSON templates.

Only resource identity fields and static dependencies are emitted. Template
values, parameters, expressions, credentials, and deployment state are never
copied to graph output. Bicep is explicitly unsupported here; it is not
compiled or executed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re

EXTRACTOR_VERSION = "arm-source-v1"
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
MAX_RESOURCES = 10_000
_SAFE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*(?:/[A-Za-z][A-Za-z0-9_.-]*)+$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESOURCE_ID = re.compile(
    r"^\[\s*resourceId\(\s*(['\"])([A-Za-z][A-Za-z0-9_.-]*(?:/[A-Za-z][A-Za-z0-9_.-]*)+)\1\s*,\s*(['\"])([A-Za-z0-9_.-]{1,128})\3\s*\)\s*\]$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArmNode:
    key: str
    kind: str
    address: str
    path: str
    declaration_index: int
    dialect: str = "arm-json"
    source_digest: str = ""
    extractor_version: str = EXTRACTOR_VERSION
    provenance: str = "declared"
    confidence: str = "high"


@dataclass(frozen=True)
class ArmEdge:
    source: str
    target: str
    relation: str
    provenance: str
    scope: str
    evidence: str


@dataclass(frozen=True)
class ArmGraph:
    nodes: tuple[ArmNode, ...]
    edges: tuple[ArmEdge, ...]
    unresolved: tuple[ArmEdge, ...]
    errors: tuple[tuple[str, str], ...]


def index_arm_text(path: str, text: str) -> ArmGraph:
    """Index a supplied ARM JSON template without evaluating any expressions."""
    path = PurePosixPath(path).as_posix()
    low = path.lower()
    if low.endswith(".bicep"):
        return ArmGraph(
            (), (), (), ((path, "Bicep source is unsupported; no compiler is invoked"),)
        )
    if not low.endswith((".json", ".arm")):
        return ArmGraph((), (), (), ((path, "unsupported ARM template suffix"),))
    if len(text.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        return ArmGraph((), (), (), ((path, "ARM template exceeds 2 MiB limit"),))

    duplicate = False

    def unique_object(pairs):
        nonlocal duplicate
        result = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        root = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return ArmGraph((), (), (), ((path, "invalid ARM JSON template"),))
    if duplicate:
        return ArmGraph((), (), (), ((path, "duplicate JSON keys are unsupported"),))
    if not isinstance(root, dict):
        return ArmGraph((), (), (), ((path, "ARM template root must be an object"),))

    raw_resources = root.get("resources", [])
    if isinstance(raw_resources, list):
        entries = [(None, item) for item in raw_resources]
    elif isinstance(raw_resources, dict):
        # Symbolic-name ARM templates use resource-name keys and allow
        # dependsOn to refer directly to those symbols.
        entries = list(raw_resources.items())
    else:
        return ArmGraph((), (), (), ((path, "ARM resources must be an array or object"),))
    if len(entries) > MAX_RESOURCES:
        return ArmGraph((), (), (), ((path, "ARM template exceeds resource limit"),))

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    nodes: list[ArmNode] = []
    unresolved: list[ArmEdge] = []
    static_deps: list[tuple[int, str, str]] = []
    symbolic: dict[str, list[ArmNode]] = {}
    address_index: dict[str, list[ArmNode]] = {}

    for index, (symbol, item) in enumerate(entries):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        name = item.get("name")
        if not isinstance(typ, str) or not _SAFE_TYPE.fullmatch(typ):
            continue
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
            # Expressions and templated names are intentionally not persisted.
            continue
        address = f"{typ}/{name}"
        identity = f"{typ.lower()}/{name.lower()}"
        key = f"infra:v1:arm-json:{path}:root:resource:{identity}:{index}"
        node = ArmNode(key, "resource", address, path, index, source_digest=digest)
        nodes.append(node)
        address_index.setdefault(identity, []).append(node)
        if isinstance(symbol, str) and _SAFE_SYMBOL.fullmatch(symbol):
            symbolic.setdefault(symbol, []).append(node)
        deps = item.get("dependsOn", [])
        if isinstance(deps, list):
            for dep in deps:
                if not isinstance(dep, str):
                    continue
                ref = None
                match = _RESOURCE_ID.fullmatch(dep)
                if match:
                    ref = f"{match.group(2).lower()}/{match.group(4).lower()}"
                    static_deps.append((index, ref, "resourceId"))
                elif isinstance(dep, str) and _SAFE_SYMBOL.fullmatch(dep):
                    static_deps.append((index, dep, "symbol"))
                else:
                    unresolved.append(
                        ArmEdge(
                            node.key,
                            _unresolved_key(path, index, dep),
                            "depends_on",
                            "declared",
                            "root",
                            "dynamic or unsupported dependency expression",
                        )
                    )

    by_index = {node.declaration_index: node for node in nodes}
    edges: list[ArmEdge] = []
    for source_index, ref, flavor in static_deps:
        source = by_index.get(source_index)
        if source is None:
            continue
        candidates = symbolic.get(ref, []) if flavor == "symbol" else address_index.get(ref, [])
        if len(candidates) == 1:
            edges.append(
                ArmEdge(source.key, candidates[0].key, "depends_on", "declared", "root", flavor)
            )
        else:
            unresolved.append(
                ArmEdge(
                    source.key,
                    _unresolved_key(path, source_index, ref),
                    "depends_on",
                    "declared",
                    "root",
                    "ambiguous or missing static dependency",
                )
            )
    return ArmGraph(tuple(nodes), tuple(edges), tuple(unresolved), ())


def _unresolved_key(path: str, index: int, reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]
    return f"infra:v1:arm-json:{path}:root:unresolved:{index}:{digest}"
