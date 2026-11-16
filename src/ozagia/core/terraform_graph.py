"""Bounded, source-only Terraform/OpenTofu structure extraction.

This deliberately stores declaration identifiers and references, never
arbitrary HCL attribute values. The sole captured value is an allowlisted
static module source path. It does not evaluate expressions, load modules, or
inspect state. The stdlib backend handles common block syntax and Terraform
JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import posixpath
from pathlib import PurePosixPath
import re

EXTRACTOR_VERSION = "terraform-source-v2"
MAX_FILE_BYTES = 2 * 1024 * 1024
SUPPORTED_SUFFIXES = (".tf", ".tf.json", ".tofu", ".tofu.json")
_BLOCK = re.compile(r"(?m)^\s*(terraform|provider|resource|data|variable|locals|output|module)\b")
_REF = re.compile(r"\b([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*){1,3})\b")
_ADDR_PREFIX = {"var": "variable", "local": "local", "module": "module", "data": "data"}
_MODULE_SOURCE = re.compile(r"[A-Za-z0-9_./-]{1,256}\Z")
_PROVIDER_ALIAS = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_MODULE_SOURCE_TARGETS = 256


@dataclass(frozen=True)
class TerraformNode:
    key: str
    kind: str
    address: str
    path: str
    start_line: int
    end_line: int
    dialect: str
    module_scope: str
    source_digest: str
    extractor_version: str = EXTRACTOR_VERSION
    provenance: str = "declared"
    confidence: str = "low"
    module_source: str | None = None


@dataclass(frozen=True)
class TerraformEdge:
    source: str
    target: str
    relation: str
    provenance: str
    scope: str
    evidence: str


@dataclass(frozen=True)
class TerraformGraph:
    nodes: tuple[TerraformNode, ...]
    edges: tuple[TerraformEdge, ...]
    unresolved: tuple[TerraformEdge, ...]
    errors: tuple[tuple[str, str], ...]


def discover_terraform_files(
    root: str | __import__("os").PathLike[str],
    *,
    skip_dirs: frozenset[str] = frozenset({".git", ".ozagia", ".terraform", "vendor"}),
) -> tuple[str, ...]:
    """Return repository-relative eligible source paths; never include state/plans."""
    import os

    base = os.fspath(root)
    found: list[str] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith("."))
        for name in files:
            low = name.lower()
            if low.endswith(SUPPORTED_SUFFIXES) and not low.endswith(
                (".tfstate", ".tfstate.backup")
            ):
                found.append(
                    PurePosixPath(os.path.relpath(os.path.join(current, name), base)).as_posix()
                )
    return tuple(sorted(found))


def _heredoc_at(text: str, i: int):
    """Return a bounded heredoc opener at i.

    HCL has no shift operator, so ``<<`` outside a quoted string or comment is
    a heredoc opener. It may appear as a direct assignment value, inside a
    list, tuple or function call; masking must not depend on the enclosing
    expression shape.
    """
    if not text.startswith("<<", i):
        return None
    j = i + 2
    indented = j < len(text) and text[j] == "-"
    if indented:
        j += 1
    while j < len(text) and text[j] in " \t":
        j += 1
    delimiter = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", text[j:])
    if delimiter is None:
        return None
    name = delimiter.group(0)
    j += len(name)
    line_end = text.find("\n", j)
    if line_end < 0:
        line_end = len(text)
        trailing = text[j:line_end]
    else:
        trailing = text[j:line_end]
    trailing = trailing.rstrip("\r")
    if trailing.strip(" \t") and not trailing.lstrip(" \t").startswith(("#", "//")):
        return None
    return i, line_end, name, indented


def _heredoc_end(text: str, opener_end: int, delimiter: str, indented: bool):
    line_start = opener_end + 1 if opener_end < len(text) else opener_end
    pattern = re.compile(
        rf"[ \t]*{re.escape(delimiter)}[ \t]*"
        if indented
        else rf"{re.escape(delimiter)}[ \t]*"
    )
    while line_start < len(text):
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = len(text)
            next_start = len(text)
        else:
            next_start = line_end + 1
        line = text[line_start:line_end].rstrip("\r")
        if pattern.fullmatch(line):
            return line_start, line_end, next_start
        line_start = next_start
    return None


def _clean_hcl(text: str) -> tuple[str, bool]:
    # Preserve offsets/newlines while masking comments, strings and heredocs.
    out = list(text)
    i = 0
    quote = False
    while i < len(text):
        if quote:
            if text[i] == "\\":
                if i + 1 < len(text):
                    out[i + 1] = " "
                out[i] = " "
                i += 2
                continue
            if text[i] == '"':
                quote = False
            if text[i] != "\n":
                out[i] = " "
            i += 1
            continue
        if text[i] == '"':
            quote = True
            out[i] = " "
            i += 1
            continue
        heredoc = _heredoc_at(text, i)
        if heredoc is not None:
            opener_start, opener_end, delimiter, indented = heredoc
            end = _heredoc_end(text, opener_end, delimiter, indented)
            if end is None:
                for k in range(opener_start, len(text)):
                    if text[k] != "\n":
                        out[k] = " "
                return "".join(out), True
            _, _, next_i = end
            for k in range(opener_start, next_i):
                if text[k] != "\n":
                    out[k] = " "
            i = next_i
            continue
        if text.startswith("//", i) or text[i] == "#":
            j = text.find("\n", i)
            if j < 0:
                j = len(text)
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = len(text) if j < 0 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out), False


def _safe_provider_alias(value: object) -> str | None:
    if isinstance(value, str) and _PROVIDER_ALIAS.fullmatch(value):
        return value
    return None


def _safe_module_source(value: object) -> str | None:
    local_path = isinstance(value, str) and value.startswith(("./", "../"))
    first_component = value.split("/", 1)[0] if isinstance(value, str) else ""
    if (
        isinstance(value, str)
        and _MODULE_SOURCE.fullmatch(value)
        and not value.startswith("/")
        and (local_path or "." not in first_component)
    ):
        return value
    return None


def _hcl_literal_attr(raw_body: str, clean_body: str, attr: str) -> str | None:
    # Find the assignment in the comment/string-masked view, then read only a
    # simple literal from the same offsets in the original source.
    assignments = list(re.finditer(rf"\b{re.escape(attr)}[ \t]*=", clean_body))
    if len(assignments) != 1:
        return None
    i = assignments[0].end()
    while i < len(raw_body) and raw_body[i] in " \t":
        i += 1
    literal = re.match(r'"([^"\\\r\n]*)"', raw_body[i:])
    return literal.group(1) if literal is not None else None


def _hcl_module_source(raw_body: str, clean_body: str) -> str | None:
    return _hcl_literal_attr(raw_body, clean_body, "source")


def _module_source_edge(node: TerraformNode, source: str) -> TerraformEdge:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    if source.startswith("./") or source.startswith("../"):
        reason = (
            "local module source; target requires repository discovery"
            if node.module_source is not None
            else "local module source is not an allowlisted static path"
        )
        scope = "module-source"
    elif source.startswith("/"):
        reason = "absolute module source is not repository-relative"
        scope = "module-source"
    else:
        reason = "remote module source"
        scope = "remote"
    return TerraformEdge(
        node.key,
        _reference_key(node.path, node.dialect, scope, digest),
        "module_source",
        "declared",
        node.module_scope,
        reason,
    )


def _blocks(text: str, clean: str):
    for match in _BLOCK.finditer(clean):
        start = match.start(1)
        i = match.end()
        # A block declaration has labels followed by an opening brace; skip
        # assignments or malformed lines rather than guessing their meaning.
        while i < len(clean) and clean[i] not in "{\n":
            i += 1
        if i >= len(clean) or clean[i] != "{":
            continue
        labels = re.findall(r'"([^"\n]*)"', text[match.end() : i])
        depth = 1
        j = i + 1
        while j < len(clean) and depth:
            if clean[j] == "{":
                depth += 1
            elif clean[j] == "}":
                depth -= 1
            j += 1
        if depth:
            continue
        yield match.group(1), labels, start, j, clean[i + 1 : j - 1]


def index_terraform_text(path: str, text: str, *, module_scope: str = "root") -> TerraformGraph:
    """Extract declarations from one .tf/.tofu source. No filesystem access."""
    path = PurePosixPath(path).as_posix()
    low = path.lower()
    if not low.endswith(SUPPORTED_SUFFIXES):
        return TerraformGraph((), (), (), ((path, "unsupported Terraform source suffix"),))
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return TerraformGraph((), (), (), ((path, "source exceeds 2 MiB limit"),))
    dialect = "opentofu" if low.endswith((".tofu", ".tofu.json")) else "terraform"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    decls: list[TerraformNode] = []
    bodies: dict[str, str] = {}
    module_sources: list[tuple[TerraformNode, str]] = []
    errors: list[tuple[str, str]] = []
    if low.endswith(".json"):
        try:
            root = json.loads(text)
        except (ValueError, RecursionError):
            return TerraformGraph((), (), (), ((path, "invalid Terraform JSON"),))
        if not isinstance(root, dict):
            return TerraformGraph((), (), (), ((path, "Terraform JSON root must be an object"),))
        for kind, key in (
            ("resource", "resource"),
            ("data", "data"),
            ("variable", "variable"),
            ("output", "output"),
            ("module", "module"),
            ("provider", "provider"),
        ):
            group = root.get(key, {})
            if not isinstance(group, dict):
                continue
            if kind in ("resource", "data"):
                for typ, entries in group.items():
                    if not isinstance(entries, dict):
                        continue
                    for name, body in entries.items():
                        if not isinstance(body, dict):
                            continue
                        address = f"{typ}.{name}"
                        if kind == "data":
                            address = f"data.{address}"
                        identity = f"{kind}:{address}"
                        node = _node(
                            path, dialect, module_scope, identity, kind, address, 1, 1, digest
                        )
                        decls.append(node)
                        bodies[node.key] = ""
            else:
                for name, body in group.items():
                    if not isinstance(body, dict):
                        continue
                    address = str(name)
                    if kind == "provider":
                        alias = _safe_provider_alias(body.get("alias"))
                        if alias:
                            address = f"{address}.{alias}"
                    identity = f"{kind}:{address}"
                    source = body.get("source") if kind == "module" else None
                    node = _node(
                        path,
                        dialect,
                        module_scope,
                        identity,
                        kind,
                        address,
                        1,
                        1,
                        digest,
                        module_source=_safe_module_source(source),
                    )
                    decls.append(node)
                    bodies[node.key] = ""
                    if kind == "module" and isinstance(source, str):
                        module_sources.append((node, source))
    else:
        clean, unterminated_heredoc = _clean_hcl(text)
        if unterminated_heredoc:
            return TerraformGraph((), (), (), ((path, "unterminated HCL heredoc"),))
        depth = 0
        for char in clean:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    return TerraformGraph((), (), (), ((path, "malformed HCL block structure"),))
        if depth:
            return TerraformGraph((), (), (), ((path, "malformed HCL block structure"),))
        for kind, labels, begin, end, body in _blocks(text, clean):
            if kind in ("locals",):
                entries = [
                    ("local", name) for name in re.findall(r"(?m)^\s*([A-Za-z_][\w-]*)\s*=", body)
                ]
                for _, name in entries:
                    addr = name
                    ident = f"local:{addr}"
                    node = _node(
                        path,
                        dialect,
                        module_scope,
                        ident,
                        "local",
                        addr,
                        text.count("\n", 0, begin) + 1,
                        text.count("\n", 0, end) + 1,
                        digest,
                    )
                    decls.append(node)
                    bodies[node.key] = clean[begin:end]
                continue
            if kind not in (
                "terraform",
                "provider",
                "resource",
                "data",
                "variable",
                "output",
                "module",
            ):
                continue
            if kind in ("resource", "data") and len(labels) != 2:
                continue
            if kind in ("provider", "variable", "output", "module") and len(labels) != 1:
                continue
            if kind == "terraform":
                continue
            address = f"{labels[0]}.{labels[1]}" if kind in ("resource", "data") else labels[0]
            if kind == "data":
                address = "data." + address
            raw_body = text[begin:end]
            clean_body = clean[begin:end]
            if kind == "provider":
                alias = _safe_provider_alias(
                    _hcl_literal_attr(raw_body, clean_body, "alias"))
                if alias:
                    address = f"{address}.{alias}"
            identity = f"{kind}:{address}"
            source = _hcl_module_source(raw_body, clean_body) if kind == "module" else None
            node = _node(
                path,
                dialect,
                module_scope,
                identity,
                kind,
                address,
                text.count("\n", 0, begin) + 1,
                text.count("\n", 0, end) + 1,
                digest,
                module_source=_safe_module_source(source),
            )
            decls.append(node)
            bodies[node.key] = clean_body
            if kind == "module" and source is not None:
                module_sources.append((node, source))
    by_ref: dict[str, TerraformNode] = {}
    for node in decls:
        by_ref[f"{node.kind}.{node.address}"] = node
        prefix = _ADDR_PREFIX.get(node.kind)
        if prefix:
            by_ref[f"{prefix}.{node.address}"] = node
        if node.kind in ("resource", "data", "provider"):
            by_ref[node.address] = node
    edges: list[TerraformEdge] = []
    unresolved: list[TerraformEdge] = []
    for node in decls:
        for match in _REF.finditer(bodies.get(node.key, "")):
            ref = match.group(1)
            # Terraform references have an address prefix and are structural
            # evidence, but expressions with indexes/dynamic scope stay open.
            target = by_ref.get(ref)
            if target is None:
                parts = ref.split(".")
                for stop in range(len(parts) - 1, 1, -1):
                    target = by_ref.get(".".join(parts[:stop]))
                    if target is not None:
                        break
            if target:
                edges.append(
                    TerraformEdge(node.key, target.key, "references", "declared", module_scope, ref)
                )
            else:
                unresolved.append(
                    TerraformEdge(
                        node.key,
                        _reference_key(path, dialect, module_scope, ref),
                        "references",
                        "declared",
                        module_scope,
                        ref,
                    )
                )
    for node, source in module_sources:
        unresolved.append(_module_source_edge(node, source))
    return TerraformGraph(
        tuple(decls), tuple(_dedupe(edges)), tuple(_dedupe(unresolved)), tuple(errors)
    )


def merge_terraform_graphs(graphs) -> TerraformGraph:
    """Merge per-file graphs and resolve bounded local modules and references.

    Ordinary references require identical dialect and module scope. Local
    module sources target declarations in their normalized repository directory
    and dialect. Duplicate address candidates remain unresolved.
    """
    nodes = tuple(node for graph in graphs for node in graph.nodes)
    all_edges = [edge for graph in graphs for edge in graph.edges]
    pending = [edge for graph in graphs for edge in graph.unresolved]
    edges = [edge for edge in all_edges if edge.relation != "references"]
    pending.extend(edge for edge in all_edges if edge.relation == "references")
    errors = tuple(error for graph in graphs for error in graph.errors)
    node_by_key = {node.key: node for node in nodes}

    declarations: dict[tuple[str, str, str], list[TerraformNode]] = {}
    for node in nodes:
        candidates = {
            f"{node.kind}.{node.address}",
            node.address,
        }
        prefix = _ADDR_PREFIX.get(node.kind)
        if prefix:
            candidates.add(f"{prefix}.{node.address}")
        for address in candidates:
            declarations.setdefault((node.dialect, node.module_scope, address), []).append(node)

    module_declarations: dict[tuple[str, str, str], list[TerraformNode]] = {}
    for node in nodes:
        if node.kind == "module":
            key = (node.dialect, node.module_scope, node.address)
            module_declarations.setdefault(key, []).append(node)

    unresolved: list[TerraformEdge] = []
    for edge in pending:
        if edge.relation == "module_source":
            source_node = node_by_key.get(edge.source)
            if source_node is None or source_node.module_source is None:
                unresolved.append(edge)
                continue
            identity = (
                source_node.dialect,
                source_node.module_scope,
                source_node.address,
            )
            if len(module_declarations.get(identity, ())) != 1:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        edge.scope,
                        "duplicate module declarations are ambiguous",
                    )
                )
                continue
            source = source_node.module_source
            if source.startswith("/"):
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        edge.scope,
                        "absolute module source is not repository-relative",
                    )
                )
                continue
            if not source.startswith(("./", "../")):
                unresolved.append(edge)
                continue
            parent = PurePosixPath(source_node.path).parent.as_posix()
            target_scope = posixpath.normpath(posixpath.join(parent, source))
            if (
                target_scope in ("", ".", "..")
                or target_scope.startswith("../")
                or target_scope.startswith("/")
            ):
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        edge.scope,
                        "local module source escapes repository root",
                    )
                )
                continue
            targets = sorted(
                (
                    node
                    for node in nodes
                    if node.dialect == source_node.dialect
                    and node.module_scope == target_scope
                    and node.path != source_node.path
                ),
                key=lambda node: node.key,
            )
            if not targets:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        target_scope,
                        "local module source has no statically discovered declarations",
                    )
                )
                continue
            for target in targets[:MAX_MODULE_SOURCE_TARGETS]:
                edges.append(
                    TerraformEdge(
                        source_node.key,
                        target.key,
                        "module_source",
                        "declared",
                        target_scope,
                        target_scope,
                    )
                )
            if len(targets) > MAX_MODULE_SOURCE_TARGETS:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        target_scope,
                        "local module source declaration limit exceeded",
                    )
                )
            continue
        if edge.relation != "references":
            unresolved.append(edge)
            continue
        # The source node carries dialect information; unresolved references
        # are intentionally scoped by the edge, not by their placeholder key.
        source = node_by_key.get(edge.source)
        if source is None:
            unresolved.append(edge)
            continue
        ref = edge.evidence
        target_candidates: list[TerraformNode] = []
        parts = ref.split(".")
        for stop in range(len(parts), 1, -1):
            key = (source.dialect, edge.scope, ".".join(parts[:stop]))
            found = declarations.get(key, [])
            if found:
                target_candidates = found
                break
        # A resource or data reference's final attribute is not part of its
        # declaration address, so longest matching declaration prefix wins.
        if len(target_candidates) == 1:
            edges.append(
                TerraformEdge(
                    edge.source,
                    target_candidates[0].key,
                    edge.relation,
                    edge.provenance,
                    edge.scope,
                    edge.evidence,
                )
            )
        else:
            unresolved.append(edge)
    return TerraformGraph(
        nodes,
        tuple(_dedupe(edges)),
        tuple(_dedupe(unresolved)),
        errors,
    )


def _node(
    path,
    dialect,
    scope,
    ident,
    kind,
    address,
    start,
    end,
    digest,
    *,
    module_source=None,
):
    key = f"infra:v1:{dialect}:{path}:{scope}:{ident}"
    return TerraformNode(
        key,
        kind,
        address,
        path,
        start,
        end,
        dialect,
        scope,
        digest,
        module_source=module_source,
    )


def _reference_key(path, dialect, scope, ref):
    safe_ref = re.sub(r"[^A-Za-z0-9_.:-]", "_", ref)[:120]
    return f"infra:v1:{dialect}:{path}:{scope}:unresolved:{safe_ref}"


def _dedupe(edges):
    return sorted(
        set(edges),
        key=lambda edge: (
            edge.source,
            edge.target,
            edge.relation,
            edge.provenance,
            edge.scope,
            edge.evidence,
        ),
    )
